from abc import ABC, abstractmethod

import numpy as np


class WrapperBase(ABC):
    def __init__(self, wrapped_layer):
        self._base_layer = wrapped_layer
        if hasattr(wrapped_layer, "_base_layer"):
            self._base_layer = wrapped_layer._base_layer
        super().__init__()

    @abstractmethod
    def _init_wrapper_params(self):
        raise NotImplementedError

    @abstractmethod
    def forward(self, z, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def backward(self, out, **kwargs):
        raise NotImplementedError

    @property
    def trainable(self):
        return self._base_layer.trainable

    @property
    def parameters(self):
        return self._base_layer.parameters

    @property
    def hyperparameters(self):
        return self._base_layer.hyperparameters

    @property
    def gradients(self):
        return self._base_layer.gradients

    @property
    def derived_variables(self):
        return self._base_layer.derived_variables

    @property
    def n_in(self):
        return self._base_layer.n_in

    @property
    def n_out(self):
        return self._base_layer.n_out

    @property
    def act_fn(self):
        return self._base_layer.act_fn

    @property
    def X(self):
        return self._base_layer.X

    def _init_params(self):
        hp = self._wrapper_hyperparameters
        if "wrappers" in self._base_layer.hyperparameters:
            self._base_layer.hyperparameters["wrappers"].append(hp)
        else:
            self._base_layer.hyperparameters["wrappers"] = [hp]

    def freeze(self):
        self._base_layer.freeze()

    def unfreeze(self):
        self._base_layer.freeze()

    def flush_gradients(self):
        assert self.trainable, "Layer is frozen"
        self._base_layer.flush_gradients()

    def update(self, lr):
        assert self.trainable, "Layer is frozen"
        self._base_layer.update(lr)
        self._base_layer.flush_gradients()

    def _set_wrapper_params(self, pdict):
        for k, v in pdict.items():
            if k in self._wrapper_hyperparameters:
                self._wrapper_hyperparameters[k] = v
        return self

    def set_params(self, summary_dict):
        return self._base_layer.set_params(summary_dict)

    def summary(self):
        return {
            "layer": self.hyperparameters["layer"],
            "layer_wrappers": [i["wrapper"] for i in self.hyperparameters["wrappers"]],
            "parameters": self.parameters,
            "hyperparameters": self.hyperparameters,
        }


class Dropout(WrapperBase):
    def __init__(self, wrapped_layer, p):
        super().__init__(wrapped_layer)
        self.p = p
        self._init_wrapper_params()
        self._init_params()

    def _init_wrapper_params(self):
        self._wrapper_hyperparameters = {"wrapper": "Dropout", "p": self.p}

    def forward(self, X):
        if self.trainable:
            dropout_mask = np.random.rand(*X.shape) >= self.p
            X = dropout_mask * X
        return self._wrapped_layer.forward(X)

    def backward(self, dLdy):
        assert self.trainable, "Layer is frozen"
        return self._wrapped_layer.backward(dLdy)
