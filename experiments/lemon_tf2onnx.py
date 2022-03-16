import os
from pathlib import Path
import argparse
from multiprocessing import Process
import time

import tensorflow as tf
from tensorflow import keras
from tensorflow.python.framework.convert_to_constants import convert_variables_to_constants_v2
import tf2onnx
from tqdm import tqdm
import numpy as np

from nnsmith.util import mkdir

tf.get_logger().setLevel('WARNING')  # Tensorflow made quiet.


def analyze_inputs_outputs(graph):
    ops = graph.get_operations()
    outputs_set = set(ops)
    inputs = []
    for op in ops:
        if len(op.inputs) == 0 and op.type == 'Placeholder':
            inputs.append(op)
        else:
            for input_tensor in op.inputs:
                if input_tensor.op in outputs_set:
                    outputs_set.remove(input_tensor.op)
    outputs = list(outputs_set)
    # Control nodes shall not be considered.
    # input like: "import/x" -> x
    # output like: "import/Identity", "import/Identity_1" -> Identity, Identity_1
    inputs = [x.name.split('/')[-1]
              for x in inputs if '_control_node' not in x.name]
    outputs = [x.name.split('/')[-1]
               for x in outputs if '_control_node' not in x.name]
    return (inputs, outputs)


def keras2tf(model):
    full_model = tf.function(lambda x: model(x))
    freeze_shape = model.inputs[0].shape

    shape_list = []
    for v in freeze_shape:
        try:
            shape_list.append(int(v))
        except TypeError as e:
            shape_list.append(1)

    full_model = full_model.get_concrete_function(
        tf.TensorSpec(tf.TensorShape(shape_list), model.inputs[0].dtype))
    frozen_func = convert_variables_to_constants_v2(full_model)

    return frozen_func.graph.as_graph_def()


def convert_tf2onnx(from_path, to_path):
    def custom_objects():
        def no_activation(x):
            return x

        def leakyrelu(x):
            return keras.activations.relu(x, alpha=0.01)

        objects = {}
        objects['no_activation'] = no_activation
        objects['leakyrelu'] = leakyrelu
        return objects

    model = keras.models.load_model(from_path, custom_objects=custom_objects())
    shape_list = []
    for v in model.inputs[0].shape:
        try:
            shape_list.append(int(v))
        except TypeError as e:
            shape_list.append(1)

    graph_def = keras2tf(model)

    with tf.Graph().as_default() as graph:
        tf.import_graph_def(graph_def)
        inps, outs = analyze_inputs_outputs(graph)

    tf2onnx.convert.from_graph_def(
        graph_def,
        input_names=[inp + f':{i}' for i, inp in enumerate(inps)],
        output_names=[out + f':{i}' for i, out in enumerate(outs)],
        opset=13,
        output_path=to_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--lemon_output_dir', type=str, required=True,
                        help='Path to the folder.')
    parser.add_argument('--onnx_dir', type=str, required=True)
    parser.add_argument('--tlimit', type=int, default=4 * 60 * 60)
    args = parser.parse_args()

    mkdir(args.onnx_dir)

    # FORMAT: {generation time cost in seconds}, {model relative path}
    # MUST RANK by GENERATION ORDER.
    config_file = open(os.path.join(args.onnx_dir, 'gentime.csv'), 'w')

    time_list = []
    file_list = []
    for file in Path(args.lemon_output_dir).rglob('*/*.h5'):
        file_list.append(file)
        time_list.append(file.stat().st_mtime)

    assert len(file_list) > 0, 'No files found in the folder!'
    print(f'{len(file_list)} files found in the folder.')

    time_arr = np.array(time_list)
    time_arr -= time_arr.min()
    idx = time_arr.argsort()

    time_span = time_arr.max() - time_arr.min()
    print(
        f'|T_last - T_first| = {time_span:.2f}s = {time_span / 60:.2f}min = {time_span / 3600:.2f}h')
    assert time_span / \
        3600 < 25, 'Are you sure your steps are correct? The time span is too long...'

    time_diffs = np.diff(np.sort(time_arr))

    cvt_start_time = time.time()

    for i in tqdm(range(len(file_list))):
        ranked_idx = idx[i]
        from_path = file_list[ranked_idx]
        to_name = os.path.split(from_path)[-1] + '.onnx'
        to_path = os.path.join(args.onnx_dir, to_name)
        try:
            tstart = time.time()
            # might crash...
            p = Process(target=convert_tf2onnx, args=(from_path, to_path))
            p.start()
            p.join()

            assert p.exitcode is not None
            if p.exitcode != 0:
                raise RuntimeError(
                    f'Conversion failed with exit code {p.exitcode}!')

            # We evaluate the end2end efficiency so we count everything.
            cvt_time = time.time() - tstart
            if i == 0:
                config_file.write(
                    f'{time_diffs.mean() + cvt_time},{to_name}\n')
            else:
                config_file.write(
                    f'{time_diffs[i - 1] + cvt_time},{to_name}\n')
        except Exception as e:
            # Models generated by LEMON are sometimes not valid.
            # We evaluate the end2end efficiency so we count everything.
            cvt_time = time.time() - tstart
            if i == 0:
                config_file.write(f'{time_diffs.mean() + cvt_time},FAILURE\n')
            else:
                config_file.write(f'{time_diffs[i - 1] + cvt_time},FAILURE\n')
            print(e)
        config_file.flush()
        if time_diffs[:i].sum() + (time.time() - cvt_start_time) > args.tlimit:
            print(f'Time limit reached. {i+1} models converted.')
            break
    config_file.close()
