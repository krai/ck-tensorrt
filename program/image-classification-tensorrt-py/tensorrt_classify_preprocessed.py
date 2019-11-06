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
MODEL_DATA_LAYOUT       = 'NCHW'
MODEL_PATH              = os.environ['CK_ENV_TENSORRT_MODEL_FILENAME']
LABELS_PATH             = os.environ['CK_CAFFE_IMAGENET_SYNSET_WORDS_TXT']

## Image normalization:
#
MODEL_NORMALIZE_DATA    = os.getenv("CK_ENV_TENSORRT_MODEL_NORMALIZE_DATA") in ('YES', 'yes', 'ON', 'on', '1')
SUBTRACT_MEAN           = os.getenv("CK_ENV_TENSORRT_MODEL_SUBTRACT_MEAN") in ('YES', 'yes', 'ON', 'on', '1')
GIVEN_CHANNEL_MEANS     = os.getenv("ML_MODEL_GIVEN_CHANNEL_MEANS", '')
if GIVEN_CHANNEL_MEANS:
    GIVEN_CHANNEL_MEANS = np.array(GIVEN_CHANNEL_MEANS.split(' '), dtype=np.float32)

SUBTRACT_MEAN = True    # valid for ResNet50

## Input image properties:
#
IMAGE_DIR               = os.getenv('CK_ENV_DATASET_IMAGENET_PREPROCESSED_DIR')
IMAGE_LIST_FILE         = os.path.join(IMAGE_DIR, os.getenv('CK_ENV_DATASET_IMAGENET_PREPROCESSED_SUBSET_FOF'))
IMAGE_DATA_TYPE         = np.dtype( os.getenv('CK_ENV_DATASET_IMAGENET_PREPROCESSED_DATA_TYPE', 'uint8') )

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
        img = np.fromfile(img_file, IMAGE_DATA_TYPE)
        img = img.reshape((MODEL_IMAGE_HEIGHT, MODEL_IMAGE_WIDTH, 3))

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
                    img -= np.mean(img)

        # Add img to batch
        batch_data.append( [img] )
        image_index += 1

    nhwc_data = np.concatenate(batch_data, axis=0)

    if MODEL_DATA_LAYOUT == 'NHWC':
        #print(nhwc_data.shape)
        return nhwc_data, image_index
    else:
        nchw_data = nhwc_data.transpose(0,3,1,2)
        #print(nchw_data.shape)
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
        print('[TRT] file {} is not found or corrupted'.format(MODEL_PATH))
        raise

    model_input_shape   = trt_engine.get_binding_shape(0)
    model_output_shape  = trt_engine.get_binding_shape(1)

    IMAGE_DATATYPE=np.float32
    h_input = cuda.pagelocked_empty(trt.volume(model_input_shape), dtype=IMAGE_DATATYPE)
    h_output = cuda.pagelocked_empty(trt.volume(model_output_shape), dtype=IMAGE_DATATYPE)
    print('Allocated device memory buffers: input_size={} output_size={}'.format(h_input.nbytes, h_output.nbytes))
    d_input = cuda.mem_alloc(h_input.nbytes)
    d_output = cuda.mem_alloc(h_output.nbytes)
    cuda_stream = cuda.Stream()

    model_classes       = model_output_shape[0]
    labels              = load_labels(LABELS_PATH)
    bg_class_offset     = model_classes-len(labels)  # 1 means the labels represent classes 1..1000 and the background class 0 has to be skipped

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
    print("Expected input shape: {}".format(model_input_shape))
    print("Output shape: {}".format(model_output_shape))
    print('Model image height: {}'.format(MODEL_IMAGE_HEIGHT))
    print('Model image width: {}'.format(MODEL_IMAGE_WIDTH))
    print('Model image channels: {}'.format(MODEL_IMAGE_CHANNELS))
    print("Background/unlabelled classes to skip: {}".format(bg_class_offset))
    print("")

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
            if FULL_REPORT or (batch_number % 10 == 0):
                print("\nBatch {} of {}".format(batch_number, BATCH_COUNT))
          
            begin_time = time.time()
            batch_data, image_index = load_preprocessed_batch(image_list, image_index)
            image_fp32 = np.array(batch_data[0]).ravel().astype(np.float32)

            load_time = time.time() - begin_time
            total_load_time += load_time
            images_loaded += BATCH_SIZE
            if FULL_REPORT:
                print("Batch loaded in %fs" % (load_time))

            # Classify image
            begin_time = time.time()

            cuda.memcpy_htod_async(d_input, image_fp32, cuda_stream)
            context.execute_async(bindings=[int(d_input), int(d_output)], stream_handle=cuda_stream.handle)
            cuda.memcpy_dtoh_async(h_output, d_output, cuda_stream)
            cuda_stream.synchronize()

            batch_results = [ h_output ]

            classification_time = time.time() - begin_time
            if FULL_REPORT:
                print("Batch classified in %fs" % (classification_time))

            total_classification_time += classification_time
            # Remember first batch prediction time
            if batch_index == 0:
                first_classification_time = classification_time

            # Process results
            for index_in_batch in range(BATCH_SIZE):
                softmax_vector = batch_results[index_in_batch][bg_class_offset:]    # skipping the background class on the left (if present)
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