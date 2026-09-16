import math
from dataclasses import dataclass, field
from itertools import repeat
from typing import Any, Callable, TypeAlias, Literal

from tensorflow.keras import models, layers
from tensorflow.python.keras.callbacks import EarlyStopping, History

ModelFitter: TypeAlias = Callable[[models.Sequential, Any, Any, Any, Any], History]


@dataclass(frozen=True)
class DefaultModelFitter:
    epochs: int = 200
    batch_size: int = 256
    callbacks: list = field(default_factory=lambda: [EarlyStopping(
        monitor='val_loss',
        patience=10,
        restore_best_weights=True
    )])
    verbose: int = 0
    validation_split: float = 0.0
    shuffle: bool = True
    class_weight: Any = None
    sample_weight: Any = None
    initial_epoch: int = 0
    steps_per_epoch: Any = None
    validation_steps: Any = None
    validation_batch_size: Any = None
    validation_freq: int = 1

    def __call__(self, model: models.Sequential, x_train, y_train, x_test, y_test) -> History:
        return model.fit(
            x=x_train,
            y=y_train,
            validation_data=(x_test, y_test),
            **self.__dict__
        )


LayerFactory: TypeAlias = Callable[[int], layers.Layer]


@dataclass(frozen=True)
class DefaultLayerFactory:
    activation: Any = 'relu'
    use_bias: bool = True
    kernel_initializer: str = "glorot_uniform"
    bias_initializer: str = "zeros"
    kernel_regularizer: Any = None
    bias_regularizer: Any = None
    activity_regularizer: Any = None
    kernel_constraint: Any = None
    bias_constraint: Any = None
    lora_rank: Any = None
    lora_alpha: Any = None
    quantization_config: Any = None

    def __call__(self, units):
        return layers.Dense(units, **self.__dict__)


OutputLayerFactory: TypeAlias = Callable[[], layers.Layer]


class DefaultOutputLayerFactory:
    def __init__(self, layer_builder: LayerFactory = DefaultLayerFactory(activation='sigmoid'), units=1):
        self.layer_builder = layer_builder
        self.units = units

    def __call__(self):
        return self.layer_builder(self.units)


ModelCompiler: TypeAlias = Callable[[models.Sequential], models.Sequential]


@dataclass(frozen=True)
class DefaultModelCompiler:
    optimizer: str = "rmsprop"
    loss: Any = None
    loss_weights: Any = None
    metrics: Any = None
    weighted_metrics: Any = None
    run_eagerly: bool = False
    steps_per_execution: int = 1
    jit_compile: str = "auto"
    auto_scale_loss: bool = True

    def __call__(self, model: models.Sequential):
        model.compile(**self.__dict__)
        return model


ModelFactory: TypeAlias = Callable[[list[int]], models.Sequential]


@dataclass(frozen=True)
class DefaultModelFactory:
    hidden_factory: LayerFactory = DefaultLayerFactory()
    output_factory: OutputLayerFactory = DefaultOutputLayerFactory()
    model_compiler: ModelCompiler = DefaultModelCompiler()

    def __call__(self, layers_config: list[int]) -> models.Sequential:
        model = models.Sequential()
        for layer_size in layers_config:
            model.add(self.hidden_factory(layer_size))
        model.add(self.output_factory())
        self.model_compiler(model)
        return model


MetricComparator: TypeAlias = Callable[[dict, dict], bool]


class DefaultMetricComparator:
    def __init__(self,
                 metric='val_accuracy',
                 threshold=0.01,
                 mode: Literal['max', 'min'] = 'max'):
        self.metric = metric
        self.threshold = threshold
        self.mode = mode

    def __call__(self, current, best):
        if self.mode == 'max':
            is_better = current[self.metric][-1] > best[self.metric][-1] * (1 + self.threshold)
        elif self.mode == 'min':
            is_better = current[self.metric][-1] < best[self.metric][-1] * (1 - self.threshold)
        else:
            raise ValueError(f"Invalid mode: {self.mode}. Use 'max' or 'min'.")
        if is_better:
            print("New best ", self.metric, " :", current[self.metric][-1])
        return is_better

_max_metrics = ('accuracy', 'val_accuracy', 'precision', 'val_precision', 'recall', 'val_recall', 'f1_score', 'val_f1_score')

class ModelComparator:
    def __init__(self, comparator: MetricComparator | str = DefaultMetricComparator()):
        if isinstance(comparator, str):
            self._comparator = DefaultMetricComparator(metric=comparator,
                                                       mode='max' if comparator in _max_metrics else 'min')
        else:
            self._comparator = comparator
        self.best_model = None
        self.best_history = None
        self.meta_history = {}

    def _set_history(self, history: History):
        h = {}
        for key, values in history.history.items():
            if key not in self.meta_history:
                self.meta_history[key] = []
            h[key] = values[-1]
            self.meta_history[key].append(values[-1])
        print("New best model found with history:", h)
        self.best_history = history

    def try_model(self, model: models.Sequential, history: History) -> bool:
        if self.best_history is None or self._comparator(history.history, self.best_history.history):
            self._set_history(history)
            self.best_model = model
            return True
        return False


class ModelSearcher:
    def __init__(self,
                 x_train,
                 y_train,
                 x_test,
                 y_test,
                 model_comparator: ModelComparator | str = ModelComparator()):
        self.x_train = x_train
        self.y_train = y_train
        self.x_test = x_test
        self.y_test = y_test
        if isinstance(model_comparator, ModelComparator):
            self._model_comparator = model_comparator
        else:
            self._model_comparator = ModelComparator(model_comparator)

    def fit(self,
            model_factory: ModelFactory = DefaultModelFactory(),
            model_fitter: ModelFitter = DefaultModelFitter(),
            layer_init: tuple[int, ...] = (16,),
            layer_growth_factor=1.5,
            add_unit_patience=3,
            add_layer_patience=1):
        layers_config = list(layer_init)
        best_config = layers_config
        _add_units_patience = add_unit_patience
        _add_layer_patience = add_layer_patience
        while True:
            print("Trying model with hidden layers:", layers_config)
            model = model_factory(layers_config)
            model.build(input_shape=self.x_train.shape)
            history = model_fitter(model, self.x_train, self.y_train, self.x_test, self.y_test)
            if self._model_comparator.try_model(model, history):
                best_config = layers_config.copy()
                layers_config[0] = math.ceil(layers_config[0] * layer_growth_factor)
                _add_layer_patience = add_layer_patience
                _add_units_patience = add_unit_patience
                continue

            # No improvement, tries to add units if patience allows
            _add_units_patience -= 1
            if _add_units_patience > 0:
                layers_config[0] = math.ceil(layers_config[0] * layer_growth_factor)
                continue

            # Tries to add a new layer if patience allows
            _add_layer_patience -= 1
            if _add_layer_patience >= 0:
                layers_config = (list(repeat(best_config[0], times=add_layer_patience - _add_layer_patience))
                                 + best_config)
                _add_units_patience = add_unit_patience
                continue

            return self._model_comparator.best_model, self._model_comparator.best_history, best_config

"""
Usage example:

    import model_optimizer as mo
    
    model_searcher = mo.ModelSearcher(X_treino, y_treino, X_val, y_val,
                                      model_comparator='val_recall')
    best_model, best_history, best_accuracy = model_searcher.fit(
        model_factory=mo.DefaultModelFactory(
            hidden_factory=mo.DefaultLayerFactory(activation='silu'),
            model_compiler=mo.DefaultModelCompiler(loss='binary_crossentropy', metrics=['accuracy', 'recall'])
        ),
        model_fitter=mo.DefaultModelFitter(epochs=200, batch_size=10000, class_weight=PESOS_CLASSES),
        layer_init=(54,42,16)
    )
"""