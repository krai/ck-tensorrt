#!/usr/bin/env python3

import json
import time
import os
import shutil
import numpy as np

import tensorrt as trt
import pycuda.driver as cuda
import pycuda.autoinit
import pycuda.tools


## Model properties:
#
MODEL_PATH              = os.environ['CK_ENV_TENSORRT_MODEL_FILENAME']
MODEL_DATA_LAYOUT       = os.getenv('ML_MODEL_DATA_LAYOUT', 'NCHW')
LABELS_PATH             = os.environ['CK_CAFFE_IMAGENET_SYNSET_WORDS_TXT']
MODEL_COLOURS_BGR       = os.getenv('ML_MODEL_COLOUR_CHANNELS_BGR', 'NO') in ('YES', 'yes', 'ON', 'on', '1')
MODEL_INPUT_DATA_TYPE   = os.getenv('ML_MODEL_INPUT_DATA_TYPE', 'float32')
MODEL_DATA_TYPE         = os.getenv('ML_MODEL_DATA_TYPE', '(unknown)')
MODEL_USE_DLA           = os.getenv('ML_MODEL_USE_DLA', 'NO') in ('YES', 'yes', 'ON', 'on', '1')
MODEL_SOFTMAX_LAYER     = os.getenv('CK_ENV_ONNX_MODEL_OUTPUT_LAYER_NAME', os.getenv('CK_ENV_TENSORFLOW_MODEL_OUTPUT_LAYER_NAME', ''))


## Internal processing:
#
INTERMEDIATE_DATA_TYPE  = np.float32    # default for internal conversion
#INTERMEDIATE_DATA_TYPE  = np.int8       # affects the accuracy a bit

## Image normalization:
#
MODEL_NORMALIZE_DATA    = os.getenv('ML_MODEL_NORMALIZE_DATA') in ('YES', 'yes', 'ON', 'on', '1')
SUBTRACT_MEAN           = os.getenv('ML_MODEL_SUBTRACT_MEAN', 'YES') in ('YES', 'yes', 'ON', 'on', '1')
GIVEN_CHANNEL_MEANS     = os.getenv('ML_MODEL_GIVEN_CHANNEL_MEANS', '')
if GIVEN_CHANNEL_MEANS:
    GIVEN_CHANNEL_MEANS = np.fromstring(GIVEN_CHANNEL_MEANS, dtype=np.float32, sep=' ').astype(INTERMEDIATE_DATA_TYPE)
    if MODEL_COLOURS_BGR:
        GIVEN_CHANNEL_MEANS = GIVEN_CHANNEL_MEANS[::-1]     # swapping Red and Blue colour channels

## Input image properties:
#
IMAGE_DIR               = os.getenv('CK_ENV_DATASET_IMAGENET_PREPROCESSED_DIR')
IMAGE_LIST_FILE         = os.path.join(IMAGE_DIR, os.getenv('CK_ENV_DATASET_IMAGENET_PREPROCESSED_SUBSET_FOF'))
IMAGE_DATA_TYPE         = os.getenv('CK_ENV_DATASET_IMAGENET_PREPROCESSED_DATA_TYPE', 'uint8')

## Writing the results out:
#
RESULTS_DIR             = os.getenv('CK_RESULTS_DIR')
FULL_REPORT             = os.getenv('CK_SILENT_MODE', '0') in ('NO', 'no', 'OFF', 'off', '0')

## Processing in batches:
#
BATCH_SIZE              = int(os.getenv('CK_BATCH_SIZE', 1))
BATCH_COUNT             = int(os.getenv('CK_BATCH_COUNT', 1))

def load_preprocessed_batch(image_list, image_index):
    batch_data = []
    for _ in range(BATCH_SIZE):
        img_file = os.path.join(IMAGE_DIR, image_list[image_index])
        img = np.fromfile(img_file, np.dtype(IMAGE_DATA_TYPE))
        img = img.reshape((MODEL_IMAGE_HEIGHT, MODEL_IMAGE_WIDTH, 3))
        if MODEL_COLOURS_BGR:
            img = img[...,::-1]     # swapping Red and Blue colour channels

        if IMAGE_DATA_TYPE != 'float32':
            img = img.astype(np.float32)

            # Normalize
            if MODEL_NORMALIZE_DATA:
                img = img/127.5 - 1.0

            # Subtract mean value
            if SUBTRACT_MEAN:
                if len(GIVEN_CHANNEL_MEANS):
                    img -= GIVEN_CHANNEL_MEANS
                else:
                    img -= np.mean(img, axis=(0,1), keepdims=True)

        if MODEL_INPUT_DATA_TYPE == 'int8' or INTERMEDIATE_DATA_TYPE==np.int8:
            img = np.clip(img, -128, 127).astype(INTERMEDIATE_DATA_TYPE)

        # Add img to batch
        batch_data.append( [img.astype(MODEL_INPUT_DATA_TYPE)] )
        image_index += 1

    nhwc_data = np.concatenate(batch_data, axis=0)
    #print('NHWC shape: {}'.format(nhwc_data.shape))

    if MODEL_DATA_LAYOUT == 'NHWC':
        return nhwc_data, image_index
    elif MODEL_DATA_LAYOUT == 'CHW4':
        dla_batch_padding = max_batch_size-len(batch_data) if MODEL_USE_DLA else 0
        chw4_data = np.pad(nhwc_data, ((0,dla_batch_padding), (0,0), (0,0), (0,1)), 'constant')
        #print('CHW4 shape: {}'.format(chw4_data.shape))
        return chw4_data, image_index
    elif MODEL_DATA_LAYOUT == 'NCHW':
        nchw_data = nhwc_data.transpose(0,3,1,2)
        #print('NCHW shape: {}'.format(nchw_data.shape))
        return nchw_data, image_index


def load_labels(labels_filepath):
    my_labels = []
    input_file = open(labels_filepath, 'r')
    for l in input_file:
        my_labels.append(l.strip())
    return my_labels


def main():
    global BATCH_SIZE
    global BATCH_COUNT
    global MODEL_DATA_LAYOUT
    global MODEL_IMAGE_HEIGHT
    global MODEL_IMAGE_WIDTH
    global max_batch_size

    setup_time_begin = time.time()

    # Load preprocessed image filenames:
    with open(IMAGE_LIST_FILE, 'r') as f:
        image_list = [ s.strip() for s in f ]

    # Cleanup results directory
    if os.path.isdir(RESULTS_DIR):
        shutil.rmtree(RESULTS_DIR)
    os.mkdir(RESULTS_DIR)

    # Load the TensorRT model from file
    default_context = pycuda.tools.make_default_context()

    TRT_LOGGER = trt.Logger(trt.Logger.WARNING)
    try:
        with open(MODEL_PATH, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
            serialized_engine = f.read()
            trt_engine = runtime.deserialize_cuda_engine(serialized_engine)
            print('[TRT] successfully loaded')
    except:
        default_context.pop()
        raise RuntimeError('TensorRT model file {} is not found or corrupted'.format(MODEL_PATH))

    max_batch_size      = trt_engine.max_batch_size

    d_inputs, h_d_outputs, model_bindings = [], [], []
    for interface_layer in trt_engine:
        dtype   = trt_engine.get_binding_dtype(interface_layer)
        shape   = trt_engine.get_binding_shape(interface_layer)
        fmt     = trt_engine.get_binding_format(trt_engine.get_binding_index(interface_layer))

        if trt_engine.binding_is_input(interface_layer) and fmt == trt.TensorFormat.CHW4:
            shape[-3] = ((shape[-3] - 1) // 4 + 1) * 4
        size    = trt.volume(shape) * max_batch_size

        dev_mem = cuda.mem_alloc(size * dtype.itemsize)
        model_bindings.append( int(dev_mem) )

        if trt_engine.binding_is_input(interface_layer):
            interface_type = 'Input'
            d_inputs.append(dev_mem)
            model_input_shape   = shape
        else:
            interface_type = 'Output'
            host_mem    = cuda.pagelocked_empty(size, trt.nptype(dtype))
            h_d_outputs.append({ 'host_mem': host_mem, 'dev_mem': dev_mem })
            if MODEL_SOFTMAX_LAYER=='' or interface_layer == MODEL_SOFTMAX_LAYER:
                model_output_shape  = shape
                h_output            = host_mem

        print("{} layer {}: dtype={}, shape={}, elements_per_max_batch={}".format(interface_type, interface_layer, dtype, shape, size))

    cuda_stream         = cuda.Stream()

    model_classes       = trt.volume(model_output_shape)
    labels              = load_labels(LABELS_PATH)
    num_layers          = trt_engine.num_layers

    if MODEL_DATA_LAYOUT == 'NHWC':
        (MODEL_IMAGE_HEIGHT, MODEL_IMAGE_WIDTH, MODEL_IMAGE_CHANNELS) = model_input_shape
    else:
        (MODEL_IMAGE_CHANNELS, MODEL_IMAGE_HEIGHT, MODEL_IMAGE_WIDTH) = model_input_shape

    print('Images dir: ' + IMAGE_DIR)
    print('Image list file: ' + IMAGE_LIST_FILE)
    print('Batch size: {}'.format(BATCH_SIZE))
    print('Batch count: {}'.format(BATCH_COUNT))
    print('Results dir: ' + RESULTS_DIR);
    print('Normalize: {}'.format(MODEL_NORMALIZE_DATA))
    print('Subtract mean: {}'.format(SUBTRACT_MEAN))
    print('Per-channel means to subtract: {}'.format(GIVEN_CHANNEL_MEANS))

    print("Data layout: {}".format(MODEL_DATA_LAYOUT) )
    print('Model image height: {}'.format(MODEL_IMAGE_HEIGHT))
    print('Model image width: {}'.format(MODEL_IMAGE_WIDTH))
    print('Model image channels: {}'.format(MODEL_IMAGE_CHANNELS))
    print('Model input data type: {}'.format(MODEL_INPUT_DATA_TYPE))
    print('Model (internal) data type: {}'.format(MODEL_DATA_TYPE))
    print('Model BGR colours: {}'.format(MODEL_COLOURS_BGR))
    print('Model max_batch_size: {}'.format(max_batch_size))
    print('Model num_layers: {}'.format(num_layers))
    print("")

    if BATCH_SIZE>max_batch_size:
        default_context.pop()
        raise RuntimeError("Desired batch_size ({}) exceeds max_batch_size of the model ({})".format(BATCH_SIZE,max_batch_size))

    setup_time = time.time() - setup_time_begin

    # Run batched mode
    test_time_begin = time.time()
    image_index = 0
    total_load_time = 0
    total_classification_time = 0
    first_classification_time = 0
    images_loaded = 0

    with trt_engine.create_execution_context() as context:
        for batch_index in range(BATCH_COUNT):
            batch_number = batch_index+1
          
            begin_time = time.time()
            batch_data, image_index = load_preprocessed_batch(image_list, image_index)
            cuda_batch_size = batch_data.shape[0]

            vectored_batch = np.array(batch_data).ravel().astype(MODEL_INPUT_DATA_TYPE)

            load_time = time.time() - begin_time
            total_load_time += load_time
            images_loaded += BATCH_SIZE

            # Classify image
            begin_time = time.time()

            cuda.memcpy_htod_async(d_inputs[0], vectored_batch, cuda_stream)    # assuming one input layer for image classification
            context.execute_async(bindings=model_bindings, batch_size=cuda_batch_size, stream_handle=cuda_stream.handle)
            for output in h_d_outputs:
                cuda.memcpy_dtoh_async(output['host_mem'], output['dev_mem'], cuda_stream)

            cuda_stream.synchronize()

            classification_time = time.time() - begin_time

            print("[batch {} of {}] loading={:.2f} ms, inference={:.2f} ms".format(
                          batch_number, BATCH_COUNT, load_time*1000, classification_time*1000))

            batch_results = np.split(h_output, max_batch_size)

            total_classification_time += classification_time
            # Remember first batch prediction time
            if batch_index == 0:
                first_classification_time = classification_time

            # Process results
            for index_in_batch in range(BATCH_SIZE):
                one_batch_result = batch_results[index_in_batch]
                if model_classes==1:
                    arg_max = one_batch_result[0]
                    softmax_vector = [0]*arg_max + [1] + [0]*(1000-arg_max-1)
                else:
                    softmax_vector = one_batch_result[-1000:]    # skipping the background class on the left (if present)
                global_index = batch_index * BATCH_SIZE + index_in_batch
                res_file = os.path.join(RESULTS_DIR, image_list[global_index])
                with open(res_file + '.txt', 'w') as f:
                    for prob in softmax_vector:
                        f.write('{}\n'.format(prob))
                
    default_context.pop()

    test_time = time.time() - test_time_begin
 
    if BATCH_COUNT > 1:
        avg_classification_time = (total_classification_time - first_classification_time) / (images_loaded - BATCH_SIZE)
    else:
        avg_classification_time = total_classification_time / images_loaded

    avg_load_time = total_load_time / images_loaded

    # Store benchmarking results:
    output_dict = {
        'setup_time_s': setup_time,
        'test_time_s': test_time,
        'images_load_time_total_s': total_load_time,
        'images_load_time_avg_s': avg_load_time,
        'prediction_time_total_s': total_classification_time,
        'prediction_time_avg_s': avg_classification_time,

        'avg_time_ms': avg_classification_time * 1000,
        'avg_fps': 1.0 / avg_classification_time,
        'batch_time_ms': avg_classification_time * 1000 * BATCH_SIZE,
        'batch_size': BATCH_SIZE,
    }
    with open('tmp-ck-timer.json', 'w') as out_file:
        json.dump(output_dict, out_file, indent=4, sort_keys=True)


if __name__ == '__main__':
    main()
