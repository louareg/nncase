from typing import Dict, List, Tuple
from itertools import product
import re
import yaml
from pathlib import Path
import numpy as np
import os
import shutil
from abc import ABCMeta, abstractmethod
import nncase
import struct
from compare_util import compare_with_ground_truth, VerboseType


class Edict:
    def __init__(self, d: Dict[str, int]) -> None:
        for name, value in d.items():
            if isinstance(value, (list, tuple)):
                setattr(self, name,
                        [Edict(x) if isinstance(x, dict) else x for x in value])
            else:
                if 'kwargs' in name:
                    setattr(self, name, value if value else dict())
                else:
                    if isinstance(value, dict):
                        setattr(self, name, Edict(value))
                    else:
                        setattr(self, name, value)

    def keys(self):
        return self.__dict__.keys()

    def items(self):
        return self.__dict__.items()

    def values(self):
        return self.__dict__.values()

    def __repr__(self, indent=0) -> str:
        s: str = ''
        for k, v in self.__dict__.items():
            s += indent * ' ' + k + ' : '
            if isinstance(v, Edict):
                s += '\n' + v.__repr__(len(s) - s.rfind('\n'))
            else:
                s += v.__repr__().replace('\n', ' ')
            s += '\n'
        return s.rstrip('\n')


def generate_random(shape: List[int], dtype: np.dtype) -> np.ndarray:
    if dtype is np.uint8:
        data = np.random.randint(0, 256, shape)
    elif dtype is np.int8:
        data = np.random.randint(-128, 128, shape)
    else:
        data = np.random.rand(*shape) * 2 - 1
    data = data.astype(dtype=dtype)
    return data


def save_array_as_txt(save_path, value_np, bit_16_represent=False):
    if bit_16_represent:
        np.save(save_path, _cast_bfloat16_then_float32(value_np))
    else:
        with open(save_path, 'w') as f:
            shape_info = "shape: (" + ",".join(str(dim)
                                               for dim in value_np.shape) + ")\n"
            f.write(shape_info)

            for val in value_np.reshape([-1]):
                f.write("%f\n" % val)
    print("----> %s" % save_path)


def _cast_bfloat16_then_float32(values: np.array):
    shape = values.shape
    values = values.reshape([-1])
    for i, value in enumerate(values):
        value = float(value)
        packed = struct.pack('!f', value)
        integers = [c for c in packed][:2] + [0, 0]
        value = struct.unpack('!f', bytes(integers))[0]
        values[i] = value

    values = values.reshape(shape)
    return values


Fuc = {
    'generate_random': generate_random
}


class TestRunner(metaclass=ABCMeta):
    def __init__(self, case_name, targets=None) -> None:
        config_root = os.path.dirname(__file__)
        with open(os.path.join(config_root, 'config.yml'), encoding='utf8') as f:
            cfg = yaml.safe_load(f)
            config = Edict(cfg)

        self.cfg = self.validte_config(config)

        case_name = case_name.replace('[', '_').replace(']', '_')
        self.case_dir = os.path.join(self.cfg.setup.root, case_name)
        self.clear(self.case_dir)

        if targets is None:
            self.cfg.case.eval[0].values = self.validate_targets(
                self.cfg.case.eval[0].values)
            self.cfg.case.infer[0].values = self.validate_targets(
                self.cfg.case.infer[0].values)
        else:
            targets = self.validate_targets(targets)
            self.cfg.case.eval[0].values = targets
            self.cfg.case.infer[0].values = targets

        self.inputs: List[Dict] = []
        self.calibs: List[Dict] = []
        self.outputs: List[Dict] = []
        self.input_paths: List[Tuple[str, str]] = []
        self.calib_paths: List[Tuple[str, str]] = []
        self.output_paths: List[Tuple[str, str]] = []
        self.num_pattern = re.compile("(\d+)")

    def validte_config(self, config):
        return config

    def validate_targets(self, targets):
        new_targets = []
        for t in targets:
            if nncase.test_target(t):
                new_targets.append(t)
            else:
                print("WARN: target[{0}] not found".format(t))
        return new_targets

    def run(self, model_path: str):
        # 这里开多线程池去跑
        # case_name = self.process_model_path_name(model_path)
        # case_dir = os.path.join(self.cfg.setup.root, case_name)
        # if not os.path.exists(case_dir):
        #     os.makedirs(case_dir)
        case_dir = os.path.dirname(model_path)
        self.run_single(self.cfg.case, case_dir, model_path)

    def process_model_path_name(self, model_path: str) -> str:
        if Path(model_path).is_file():
            case_name = Path(model_path)
            return '_'.join(str(case_name.parent).split('/') + [case_name.stem])
        return model_path

    def clear(self, case_dir):
        in_ci = os.getenv('CI', False)
        if in_ci:
            if os.path.exists(self.cfg.setup.root):
                shutil.rmtree(self.cfg.setup.root)
        else:
            if os.path.exists(case_dir):
                shutil.rmtree(case_dir)
        os.makedirs(case_dir)

    @abstractmethod
    def parse_model_input_output(self, model_path: str):
        pass

    @abstractmethod
    def cpu_infer(self, case_dir: str, model_content: bytes):
        pass

    @abstractmethod
    def import_model(self, compiler, model_content, import_options):
        pass

    def run_single(self, cfg, case_dir: str, model_file: str):
        if not self.inputs:
            self.parse_model_input_output(model_file)
        self.generate_data(cfg.generate_inputs, case_dir,
                           self.inputs, self.input_paths, 'input')
        self.generate_data(cfg.generate_calibs, case_dir,
                           self.calibs, self.calib_paths, 'calib')
        self.cpu_infer(case_dir, model_file)
        import_options, compile_options = self.get_compiler_options(cfg, model_file)
        model_content = self.read_model_file(model_file)
        self.run_evaluator(cfg, case_dir, import_options, compile_options, model_content)
        self.run_inference(cfg, case_dir, import_options, compile_options, model_content)

    def get_compiler_options(self, cfg, model_file):
        import_options = nncase.ImportOptions(**cfg.importer_opt.kwargs)
        if os.path.splitext(model_file)[-1] == ".tflite":
            import_options.input_layout = "NHWC"
            import_options.output_layout = "NHWC"
        elif os.path.splitext(model_file)[-1] == ".onnx":
            import_options.input_layout = "NCHW"
            import_options.output_layout = "NCHW"

        compile_options = nncase.CompileOptions()
        for k, v in cfg.compile_opt.kwargs.items():
            e = '"'
            exec(
                f'compile_options.{k} = {e + v + e if isinstance(v, str) else v}')
        return import_options, compile_options

    def run_evaluator(self, cfg, case_dir, import_options, compile_options, model_content):
        names, args = TestRunner.split_value(cfg.eval)
        for combine_args in product(*args):
            dict_args = dict(zip(names, combine_args))
            eval_output_paths = self.generate_evaluates(
                cfg, case_dir, import_options,
                compile_options, model_content, dict_args)
            assert self.compare_results(
                self.output_paths, eval_output_paths, dict_args)

    def run_inference(self, cfg, case_dir, import_options, compile_options, model_content):
        names, args = TestRunner.split_value(cfg.infer)
        for combine_args in product(*args):
            dict_args = dict(zip(names, combine_args))
            if dict_args['ptq'] and len(self.inputs) > 1:
                continue

            infer_output_paths = self.nncase_infer(
                cfg, case_dir, import_options,
                compile_options, model_content, dict_args)
            assert self.compare_results(
                self.output_paths, infer_output_paths, dict_args)

    @staticmethod
    def split_value(kwcfg: List[Dict[str, str]]) -> Tuple[List[str], List[str]]:
        arg_names = []
        arg_values = []
        for d in kwcfg:
            arg_names.append(d.name)
            arg_values.append(d.values)
        return (arg_names, arg_values)

    def read_model_file(self, model_file: str) -> bytes:
        with open(model_file, 'rb') as f:
            model_content = f.read()
        return model_content

    @staticmethod
    def kwargs_to_path(path: str, kwargs: Dict[str, str]):
        for k, v in kwargs.items():
            if isinstance(v, str):
                path = os.path.join(path, v)
            elif isinstance(v, bool):
                path = os.path.join(path, ('' if v else 'no') + k)
        return path

    def generate_evaluates(self, cfg, case_dir: str,
                           import_options: nncase.ImportOptions,
                           compile_options: nncase.CompileOptions,
                           model_content: bytes, kwargs: Dict[str, str]
                           ) -> List[Tuple[str, str]]:
        eval_dir = TestRunner.kwargs_to_path(
            os.path.join(case_dir, 'eval'), kwargs)
        compile_options.target = kwargs['target']
        compile_options.dump_dir = eval_dir
        compiler = nncase.Compiler(compile_options)
        self.import_model(compiler, model_content, import_options)
        evaluator = compiler.create_evaluator(3)
        eval_output_paths = []
        for i in range(len(self.inputs)):
            input_tensor = nncase.RuntimeTensor.from_numpy(
                self.inputs[i]['data'])
            input_tensor.copy_to(evaluator.get_input_tensor(i))
            evaluator.run()

        for i in range(evaluator.outputs_size):
            result = evaluator.get_output_tensor(i).to_numpy()
            eval_output_paths.append((
                os.path.join(eval_dir, f'nncase_result_{i}.bin'),
                os.path.join(eval_dir, f'nncase_result_{i}.txt')))
            result.tofile(eval_output_paths[-1][0])
            save_array_as_txt(eval_output_paths[-1][1], result)
        return eval_output_paths

    def nncase_infer(self, cfg, case_dir: str,
                     import_options: nncase.ImportOptions,
                     compile_options: nncase.CompileOptions,
                     model_content: bytes, kwargs: Dict[str, str]
                     ) -> List[Tuple[str, str]]:
        infer_dir = TestRunner.kwargs_to_path(
            os.path.join(case_dir, 'infer'), kwargs)
        compile_options.target = kwargs['target']
        compile_options.dump_dir = infer_dir
        compiler = nncase.Compiler(compile_options)
        self.import_model(compiler, model_content, import_options)
        if kwargs['ptq']:
            ptq_options = nncase.PTQTensorOptions()
            ptq_options.set_tensor_data(np.asarray([sample['data'] for sample in self.calibs]).tobytes())
            ptq_options.samples_count = cfg.generate_calibs.batch_size
            ptq_options.input_mean = cfg.ptq_opt.kwargs['input_mean']
            ptq_options.input_std = cfg.ptq_opt.kwargs['input_std']

            compiler.use_ptq(ptq_options)
        compiler.compile()
        kmodel = compiler.gencode_tobytes()
        with open(os.path.join(infer_dir, 'test.kmodel'), 'wb') as f:
            f.write(kmodel)
        sim = nncase.Simulator()
        sim.load_model(kmodel)
        infer_output_paths: List[np.ndarray] = []
        for i in range(len(self.inputs)):
            sim.set_input_tensor(
                i, nncase.RuntimeTensor.from_numpy(self.inputs[i]['data']))

        sim.run()

        for i in range(sim.outputs_size):
            result = sim.get_output_tensor(i).to_numpy()
            infer_output_paths.append((
                os.path.join(infer_dir, f'nncase_result_{i}.bin'),
                os.path.join(infer_dir, f'nncase_result_{i}.txt')))
            result.tofile(infer_output_paths[-1][0])
            save_array_as_txt(infer_output_paths[-1][1], result)
        return infer_output_paths

    def on_test_start(self) -> None:
        pass

    def generate_data(self, cfg, case_dir: str, inputs: List[Dict], path_list: List[str], name: str):
        for n in range(cfg.numbers):
            i = 0
            for input in inputs:
                shape = input['shape']
                shape[0] *= cfg.batch_size
                data = Fuc[cfg.name](shape, input['dtype'])

                path_list.append(
                    (os.path.join(case_dir, f'{name}_{n}_{i}.bin'),
                     os.path.join(case_dir, f'{name}_{n}_{i}.txt')))
                data.tofile(path_list[-1][0])
                save_array_as_txt(path_list[-1][1], data)
                i += 1
                input['data'] = data

    def process_input(self, inputs: List[np.array], **kwargs) -> None:
        pass

    def process_output(self, outputs: List[np.array], **kwargs) -> None:
        pass

    def on_test_end(self) -> None:
        pass

    def compare_results(self,
                        ref_ouputs: List[Tuple[str]],
                        test_outputs: List[Tuple[str]],
                        kwargs: Dict[str, str]):
        for ref_file, test_file in zip(ref_ouputs, test_outputs):
            judge = compare_with_ground_truth(test_file[1],
                                              ref_file[1],
                                              state=0,
                                              verbose=VerboseType.PRINT_RESULT)
            name_list = test_file[1].split('/')
            kw_names = ' '.join(name_list[-len(kwargs) - 2:-1])
            i = self.num_pattern.findall(name_list[-1])
            if judge.is_good():
                result = "\nPass [ {0} ] Output: {1}!!\n".format(kw_names, i)
                print(result)
                with open(os.path.join(self.case_dir, 'test_result.txt'), 'a+') as f:
                    f.write(result)
            else:
                result = "\nFail [ {0} ] Output: {1}!!\n".format(kw_names, i)
                print(result)
                with open(os.path.join(self.case_dir, 'test_result.txt'), 'a+') as f:
                    f.write(result)
                return False
        return True
