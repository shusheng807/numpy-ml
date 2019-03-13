import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np

#######################################################################
#       Gold-standard implementations for testing custom layers       #
#                       (Requires Pytorch)                            #
#######################################################################


def torchify(var, requires_grad=True):
    return torch.autograd.Variable(torch.FloatTensor(var), requires_grad=requires_grad)


def torch_gradient_generator(fn, **kwargs):
    def get_grad(z):
        z1 = torch.autograd.Variable(torch.FloatTensor(z), requires_grad=True)
        z2 = fn(z1, **kwargs).sum()
        z2.backward()
        grad = z1.grad.numpy()
        return grad

    return get_grad


def torch_xe_grad(y, z):
    z = torch.autograd.Variable(torch.FloatTensor(z), requires_grad=True)
    y = torch.LongTensor(y.argmax(axis=1))
    loss = F.cross_entropy(z, y, size_average=False).sum()
    loss.backward()
    grad = z.grad.numpy()
    return grad


def torch_mse_grad(y, z, act_fn):
    y = torch.FloatTensor(y)
    z = torch.autograd.Variable(torch.FloatTensor(z), requires_grad=True)
    y_pred = act_fn(z)
    loss = F.mse_loss(y_pred, y, size_average=False).sum()
    loss.backward()
    grad = z.grad.numpy()
    return grad


class TorchLinearActivation(nn.Module):
    def __init__(self):
        super(TorchLinearActivation, self).__init__()
        pass

    @staticmethod
    def forward(input):
        return input

    @staticmethod
    def backward(grad_output):
        return torch.ones_like(grad_output)


class TorchBatchNormLayer(nn.Module):
    def __init__(self, n_in, params, mode, momentum=0.9, epsilon=1e-5):
        super(TorchBatchNormLayer, self).__init__()

        scaler = params["scaler"]
        intercept = params["intercept"]

        if mode == "1D":
            self.layer1 = nn.BatchNorm1d(
                num_features=n_in, momentum=1 - momentum, eps=epsilon, affine=True
            )
        elif mode == "2D":
            self.layer1 = nn.BatchNorm2d(
                num_features=n_in, momentum=1 - momentum, eps=epsilon, affine=True
            )

        self.layer1.weight = nn.Parameter(torch.FloatTensor(scaler))
        self.layer1.bias = nn.Parameter(torch.FloatTensor(intercept))

    def forward(self, X):
        # (N, H, W, C) -> (N, C, H, W)
        if X.ndim == 4:
            X = np.moveaxis(X, [0, 1, 2, 3], [0, -2, -1, -3])

        if not isinstance(X, torch.Tensor):
            X = torchify(X)

        self.X = X
        self.Y = self.layer1(self.X)
        self.Y.retain_grad()

    def extract_grads(self, X, Y_true=None):
        self.forward(X)

        if isinstance(Y_true, np.ndarray):
            Y_true = np.moveaxis(Y_true, [0, 1, 2, 3], [0, -2, -1, -3])
            self.loss1 = (
                0.5 * F.mse_loss(self.Y, torchify(Y_true), size_average=False).sum()
            )
        else:
            self.loss1 = self.Y.sum()

        self.loss1.backward()

        X_np = self.X.detach().numpy()
        Y_np = self.Y.detach().numpy()
        dX_np = self.X.grad.numpy()
        dY_np = self.Y.grad.numpy()

        if self.X.dim() == 4:
            orig, X_swap = [0, 1, 2, 3], [0, -1, -3, -2]
            if isinstance(Y_true, np.ndarray):
                Y_true = np.moveaxis(Y_true, orig, X_swap)
            X_np = np.moveaxis(X_np, orig, X_swap)
            Y_np = np.moveaxis(Y_np, orig, X_swap)
            dX_np = np.moveaxis(dX_np, orig, X_swap)
            dY_np = np.moveaxis(dY_np, orig, X_swap)

        grads = {
            "loss": self.loss1.detach().numpy(),
            "X": X_np,
            "momentum": 1 - self.layer1.momentum,
            "epsilon": self.layer1.eps,
            "intercept": self.layer1.bias.detach().numpy(),
            "scaler": self.layer1.weight.detach().numpy(),
            "running_mean": self.layer1.running_mean.detach().numpy(),
            "running_var": self.layer1.running_var.detach().numpy(),
            "y": Y_np,
            "dLdy": dY_np,
            "dLdIntercept": self.layer1.bias.grad.numpy(),
            "dLdScaler": self.layer1.weight.grad.numpy(),
            "dLdX": dX_np,
        }
        if isinstance(Y_true, np.ndarray):
            grads["Y_true"] = Y_true
        return grads


class TorchAddLayer(nn.Module):
    def __init__(self, act_fn, **kwargs):
        super(TorchAddLayer, self).__init__()
        self.act_fn = act_fn

    def forward(self, Xs):
        self.Xs = []
        x = Xs[0].copy()
        if not isinstance(x, torch.Tensor):
            x = torchify(x)

        self.sum = x.clone()
        x.retain_grad()
        self.Xs.append(x)

        for i in range(1, len(Xs)):
            x = Xs[i]
            if not isinstance(x, torch.Tensor):
                x = torchify(x)

            x.retain_grad()
            self.Xs.append(x)
            self.sum += x

        self.sum.retain_grad()
        self.Y = self.act_fn(self.sum)
        self.Y.retain_grad()
        return self.Y

    def extract_grads(self, X):
        self.forward(X)
        self.loss = self.Y.sum()
        self.loss.backward()
        grads = {
            "Xs": X,
            "Sum": self.sum.detach().numpy(),
            "Y": self.Y.detach().numpy(),
            "dLdY": self.Y.grad.numpy(),
            "dLdSum": self.sum.grad.numpy(),
        }
        grads.update(
            {"dLdX{}".format(i + 1): xi.grad.numpy() for i, xi in enumerate(self.Xs)}
        )
        return grads


class TorchSkipConnectionIdentity(nn.Module):
    def __init__(self, act_fn, pad1, pad2, params, hparams, momentum=0.9, epsilon=1e-5):
        super(TorchSkipConnectionIdentity, self).__init__()

        self.conv1 = nn.Conv2d(
            hparams["in_channels"],
            hparams["out_channels"],
            hparams["kernel_shape1"],
            padding=pad1,
            stride=hparams["stride1"],
            bias=True,
        )

        self.act_fn = act_fn

        self.batchnorm1 = nn.BatchNorm2d(
            num_features=hparams["out_channels"],
            momentum=1 - momentum,
            eps=epsilon,
            affine=True,
        )

        self.conv2 = nn.Conv2d(
            hparams["out_channels"],
            hparams["out_channels"],
            hparams["kernel_shape2"],
            padding=pad2,
            stride=hparams["stride2"],
            bias=True,
        )

        self.batchnorm2 = nn.BatchNorm2d(
            num_features=hparams["out_channels"],
            momentum=1 - momentum,
            eps=epsilon,
            affine=True,
        )

        orig, W_swap = [0, 1, 2, 3], [-2, -1, -3, -4]
        # (f[0], f[1], n_in, n_out) -> (n_out, n_in, f[0], f[1])
        W = params["components"]["conv1"]["W"]
        b = params["components"]["conv1"]["b"]
        W = np.moveaxis(W, orig, W_swap)
        assert self.conv1.weight.shape == W.shape
        assert self.conv1.bias.shape == b.flatten().shape
        self.conv1.weight = nn.Parameter(torch.FloatTensor(W))
        self.conv1.bias = nn.Parameter(torch.FloatTensor(b.flatten()))

        scaler = params["components"]["batchnorm1"]["scaler"]
        intercept = params["components"]["batchnorm1"]["intercept"]
        self.batchnorm1.weight = nn.Parameter(torch.FloatTensor(scaler))
        self.batchnorm1.bias = nn.Parameter(torch.FloatTensor(intercept))

        # (f[0], f[1], n_in, n_out) -> (n_out, n_in, f[0], f[1])
        W = params["components"]["conv2"]["W"]
        b = params["components"]["conv2"]["b"]
        W = np.moveaxis(W, orig, W_swap)
        assert self.conv2.weight.shape == W.shape
        assert self.conv2.bias.shape == b.flatten().shape
        self.conv2.weight = nn.Parameter(torch.FloatTensor(W))
        self.conv2.bias = nn.Parameter(torch.FloatTensor(b.flatten()))

        scaler = params["components"]["batchnorm2"]["scaler"]
        intercept = params["components"]["batchnorm2"]["intercept"]
        self.batchnorm2.weight = nn.Parameter(torch.FloatTensor(scaler))
        self.batchnorm2.bias = nn.Parameter(torch.FloatTensor(intercept))

    def forward(self, X):
        if not isinstance(X, torch.Tensor):
            # (N, H, W, C) -> (N, C, H, W)
            X = np.moveaxis(X, [0, 1, 2, 3], [0, -2, -1, -3])
            X = torchify(X)

        self.X = X
        self.X.retain_grad()

        self.conv1_out = self.conv1(self.X)
        self.conv1_out.retain_grad()

        self.act_fn1_out = self.act_fn(self.conv1_out)
        self.act_fn1_out.retain_grad()

        self.batchnorm1_out = self.batchnorm1(self.act_fn1_out)
        self.batchnorm1_out.retain_grad()

        self.conv2_out = self.conv2(self.batchnorm1_out)
        self.conv2_out.retain_grad()

        self.batchnorm2_out = self.batchnorm2(self.conv2_out)
        self.batchnorm2_out.retain_grad()

        self.layer3_in = self.batchnorm2_out + self.X
        self.layer3_in.retain_grad()

        self.Y = self.act_fn(self.layer3_in)
        self.Y.retain_grad()

    def extract_grads(self, X):
        self.forward(X)
        self.loss = self.Y.sum()
        self.loss.backward()

        orig, X_swap, W_swap = [0, 1, 2, 3], [0, -1, -3, -2], [-1, -2, -4, -3]
        grads = {
            # layer parameters
            "conv1_W": np.moveaxis(self.conv1.weight.detach().numpy(), orig, W_swap),
            "conv1_b": self.conv1.bias.detach().numpy().reshape(1, 1, 1, -1),
            "bn1_intercept": self.batchnorm1.bias.detach().numpy(),
            "bn1_scaler": self.batchnorm1.weight.detach().numpy(),
            "bn1_running_mean": self.batchnorm1.running_mean.detach().numpy(),
            "bn1_running_var": self.batchnorm1.running_var.detach().numpy(),
            "conv2_W": np.moveaxis(self.conv2.weight.detach().numpy(), orig, W_swap),
            "conv2_b": self.conv2.bias.detach().numpy().reshape(1, 1, 1, -1),
            "bn2_intercept": self.batchnorm2.bias.detach().numpy(),
            "bn2_scaler": self.batchnorm2.weight.detach().numpy(),
            "bn2_running_mean": self.batchnorm2.running_mean.detach().numpy(),
            "bn2_running_var": self.batchnorm2.running_var.detach().numpy(),
            # layer inputs/outputs (forward step)
            "X": np.moveaxis(self.X.detach().numpy(), orig, X_swap),
            "conv1_out": np.moveaxis(self.conv1_out.detach().numpy(), orig, X_swap),
            "act1_out": np.moveaxis(self.act_fn1_out.detach().numpy(), orig, X_swap),
            "bn1_out": np.moveaxis(self.batchnorm1_out.detach().numpy(), orig, X_swap),
            "conv2_out": np.moveaxis(self.conv2_out.detach().numpy(), orig, X_swap),
            "bn2_out": np.moveaxis(self.batchnorm2_out.detach().numpy(), orig, X_swap),
            "add_out": np.moveaxis(self.layer3_in.detach().numpy(), orig, X_swap),
            "Y": np.moveaxis(self.Y.detach().numpy(), orig, X_swap),
            # layer gradients (backward step)
            "dLdY": np.moveaxis(self.Y.grad.numpy(), orig, X_swap),
            "dLdAdd": np.moveaxis(self.layer3_in.grad.numpy(), orig, X_swap),
            "dLdBn2_out": np.moveaxis(self.batchnorm2_out.grad.numpy(), orig, X_swap),
            "dLdConv2_out": np.moveaxis(self.conv2_out.grad.numpy(), orig, X_swap),
            "dLdBn1_out": np.moveaxis(self.batchnorm1_out.grad.numpy(), orig, X_swap),
            "dLdActFn1_out": np.moveaxis(self.act_fn1_out.grad.numpy(), orig, X_swap),
            "dLdConv1_out": np.moveaxis(self.act_fn1_out.grad.numpy(), orig, X_swap),
            "dLdX": np.moveaxis(self.X.grad.numpy(), orig, X_swap),
            # layer parameter gradients (backward step)
            "dLdBn2_intercept": self.batchnorm2.bias.grad.numpy(),
            "dLdBn2_scaler": self.batchnorm2.weight.grad.numpy(),
            "dLdConv2_W": np.moveaxis(self.conv2.weight.grad.numpy(), orig, W_swap),
            "dLdConv2_b": self.conv2.bias.grad.numpy().reshape(1, 1, 1, -1),
            "dLdBn1_intercept": self.batchnorm1.bias.grad.numpy(),
            "dLdBn1_scaler": self.batchnorm1.weight.grad.numpy(),
            "dLdConv1_W": np.moveaxis(self.conv1.weight.grad.numpy(), orig, W_swap),
            "dLdConv1_b": self.conv1.bias.grad.numpy().reshape(1, 1, 1, -1),
        }
        return grads


class TorchSkipConnectionConv(nn.Module):
    def __init__(
        self, act_fn, pad1, pad2, pad_skip, params, hparams, momentum=0.9, epsilon=1e-5
    ):
        super(TorchSkipConnectionConv, self).__init__()

        self.conv1 = nn.Conv2d(
            hparams["in_channels"],
            hparams["out_channels1"],
            hparams["kernel_shape1"],
            padding=pad1,
            stride=hparams["stride1"],
            bias=True,
        )

        self.act_fn = act_fn

        self.batchnorm1 = nn.BatchNorm2d(
            num_features=hparams["out_channels1"],
            momentum=1 - momentum,
            eps=epsilon,
            affine=True,
        )

        self.conv2 = nn.Conv2d(
            hparams["out_channels1"],
            hparams["out_channels2"],
            hparams["kernel_shape2"],
            padding=pad2,
            stride=hparams["stride2"],
            bias=True,
        )

        self.batchnorm2 = nn.BatchNorm2d(
            num_features=hparams["out_channels2"],
            momentum=1 - momentum,
            eps=epsilon,
            affine=True,
        )

        self.conv_skip = nn.Conv2d(
            hparams["in_channels"],
            hparams["out_channels2"],
            hparams["kernel_shape_skip"],
            padding=pad_skip,
            stride=hparams["stride_skip"],
            bias=True,
        )

        self.batchnorm_skip = nn.BatchNorm2d(
            num_features=hparams["out_channels2"],
            momentum=1 - momentum,
            eps=epsilon,
            affine=True,
        )

        orig, W_swap = [0, 1, 2, 3], [-2, -1, -3, -4]
        # (f[0], f[1], n_in, n_out) -> (n_out, n_in, f[0], f[1])
        W = params["components"]["conv1"]["W"]
        b = params["components"]["conv1"]["b"]
        W = np.moveaxis(W, orig, W_swap)
        assert self.conv1.weight.shape == W.shape
        assert self.conv1.bias.shape == b.flatten().shape
        self.conv1.weight = nn.Parameter(torch.FloatTensor(W))
        self.conv1.bias = nn.Parameter(torch.FloatTensor(b.flatten()))

        scaler = params["components"]["batchnorm1"]["scaler"]
        intercept = params["components"]["batchnorm1"]["intercept"]
        self.batchnorm1.weight = nn.Parameter(torch.FloatTensor(scaler))
        self.batchnorm1.bias = nn.Parameter(torch.FloatTensor(intercept))

        # (f[0], f[1], n_in, n_out) -> (n_out, n_in, f[0], f[1])
        W = params["components"]["conv2"]["W"]
        b = params["components"]["conv2"]["b"]
        W = np.moveaxis(W, orig, W_swap)
        assert self.conv2.weight.shape == W.shape
        assert self.conv2.bias.shape == b.flatten().shape
        self.conv2.weight = nn.Parameter(torch.FloatTensor(W))
        self.conv2.bias = nn.Parameter(torch.FloatTensor(b.flatten()))

        scaler = params["components"]["batchnorm2"]["scaler"]
        intercept = params["components"]["batchnorm2"]["intercept"]
        self.batchnorm2.weight = nn.Parameter(torch.FloatTensor(scaler))
        self.batchnorm2.bias = nn.Parameter(torch.FloatTensor(intercept))

        W = params["components"]["conv_skip"]["W"]
        b = params["components"]["conv_skip"]["b"]
        W = np.moveaxis(W, orig, W_swap)
        assert self.conv_skip.weight.shape == W.shape
        assert self.conv_skip.bias.shape == b.flatten().shape
        self.conv_skip.weight = nn.Parameter(torch.FloatTensor(W))
        self.conv_skip.bias = nn.Parameter(torch.FloatTensor(b.flatten()))

        scaler = params["components"]["batchnorm_skip"]["scaler"]
        intercept = params["components"]["batchnorm_skip"]["intercept"]
        self.batchnorm_skip.weight = nn.Parameter(torch.FloatTensor(scaler))
        self.batchnorm_skip.bias = nn.Parameter(torch.FloatTensor(intercept))

    def forward(self, X):
        if not isinstance(X, torch.Tensor):
            # (N, H, W, C) -> (N, C, H, W)
            X = np.moveaxis(X, [0, 1, 2, 3], [0, -2, -1, -3])
            X = torchify(X)

        self.X = X
        self.X.retain_grad()

        self.conv1_out = self.conv1(self.X)
        self.conv1_out.retain_grad()

        self.act_fn1_out = self.act_fn(self.conv1_out)
        self.act_fn1_out.retain_grad()

        self.batchnorm1_out = self.batchnorm1(self.act_fn1_out)
        self.batchnorm1_out.retain_grad()

        self.conv2_out = self.conv2(self.batchnorm1_out)
        self.conv2_out.retain_grad()

        self.batchnorm2_out = self.batchnorm2(self.conv2_out)
        self.batchnorm2_out.retain_grad()

        self.c_skip_out = self.conv_skip(self.X)
        self.c_skip_out.retain_grad()

        self.bn_skip_out = self.batchnorm_skip(self.c_skip_out)
        self.bn_skip_out.retain_grad()

        self.layer3_in = self.batchnorm2_out + self.bn_skip_out
        self.layer3_in.retain_grad()

        self.Y = self.act_fn(self.layer3_in)
        self.Y.retain_grad()

    def extract_grads(self, X):
        self.forward(X)
        self.loss = self.Y.sum()
        self.loss.backward()

        orig, X_swap, W_swap = [0, 1, 2, 3], [0, -1, -3, -2], [-1, -2, -4, -3]
        grads = {
            # layer parameters
            "conv1_W": np.moveaxis(self.conv1.weight.detach().numpy(), orig, W_swap),
            "conv1_b": self.conv1.bias.detach().numpy().reshape(1, 1, 1, -1),
            "bn1_intercept": self.batchnorm1.bias.detach().numpy(),
            "bn1_scaler": self.batchnorm1.weight.detach().numpy(),
            "bn1_running_mean": self.batchnorm1.running_mean.detach().numpy(),
            "bn1_running_var": self.batchnorm1.running_var.detach().numpy(),
            "conv2_W": np.moveaxis(self.conv2.weight.detach().numpy(), orig, W_swap),
            "conv2_b": self.conv2.bias.detach().numpy().reshape(1, 1, 1, -1),
            "bn2_intercept": self.batchnorm2.bias.detach().numpy(),
            "bn2_scaler": self.batchnorm2.weight.detach().numpy(),
            "bn2_running_mean": self.batchnorm2.running_mean.detach().numpy(),
            "bn2_running_var": self.batchnorm2.running_var.detach().numpy(),
            "conv_skip_W": np.moveaxis(
                self.conv_skip.weight.detach().numpy(), orig, W_swap
            ),
            "conv_skip_b": self.conv_skip.bias.detach().numpy().reshape(1, 1, 1, -1),
            "bn_skip_intercept": self.batchnorm_skip.bias.detach().numpy(),
            "bn_skip_scaler": self.batchnorm_skip.weight.detach().numpy(),
            "bn_skip_running_mean": self.batchnorm_skip.running_mean.detach().numpy(),
            "bn_skip_running_var": self.batchnorm_skip.running_var.detach().numpy(),
            # layer inputs/outputs (forward step)
            "X": np.moveaxis(self.X.detach().numpy(), orig, X_swap),
            "conv1_out": np.moveaxis(self.conv1_out.detach().numpy(), orig, X_swap),
            "act1_out": np.moveaxis(self.act_fn1_out.detach().numpy(), orig, X_swap),
            "bn1_out": np.moveaxis(self.batchnorm1_out.detach().numpy(), orig, X_swap),
            "conv2_out": np.moveaxis(self.conv2_out.detach().numpy(), orig, X_swap),
            "bn2_out": np.moveaxis(self.batchnorm2_out.detach().numpy(), orig, X_swap),
            "conv_skip_out": np.moveaxis(
                self.c_skip_out.detach().numpy(), orig, X_swap
            ),
            "bn_skip_out": np.moveaxis(self.bn_skip_out.detach().numpy(), orig, X_swap),
            "add_out": np.moveaxis(self.layer3_in.detach().numpy(), orig, X_swap),
            "Y": np.moveaxis(self.Y.detach().numpy(), orig, X_swap),
            # layer gradients (backward step)
            "dLdY": np.moveaxis(self.Y.grad.numpy(), orig, X_swap),
            "dLdAdd": np.moveaxis(self.layer3_in.grad.numpy(), orig, X_swap),
            "dLdBnSkip_out": np.moveaxis(self.bn_skip_out.grad.numpy(), orig, X_swap),
            "dLdConvSkip_out": np.moveaxis(self.c_skip_out.grad.numpy(), orig, X_swap),
            "dLdBn2_out": np.moveaxis(self.batchnorm2_out.grad.numpy(), orig, X_swap),
            "dLdConv2_out": np.moveaxis(self.conv2_out.grad.numpy(), orig, X_swap),
            "dLdBn1_out": np.moveaxis(self.batchnorm1_out.grad.numpy(), orig, X_swap),
            "dLdActFn1_out": np.moveaxis(self.act_fn1_out.grad.numpy(), orig, X_swap),
            "dLdConv1_out": np.moveaxis(self.act_fn1_out.grad.numpy(), orig, X_swap),
            "dLdX": np.moveaxis(self.X.grad.numpy(), orig, X_swap),
            # layer parameter gradients (backward step)
            "dLdBnSkip_intercept": self.batchnorm_skip.bias.grad.numpy(),
            "dLdBnSkip_scaler": self.batchnorm_skip.weight.grad.numpy(),
            "dLdConvSkip_W": np.moveaxis(
                self.conv_skip.weight.grad.numpy(), orig, W_swap
            ),
            "dLdConvSkip_b": self.conv_skip.bias.grad.numpy().reshape(1, 1, 1, -1),
            "dLdBn2_intercept": self.batchnorm2.bias.grad.numpy(),
            "dLdBn2_scaler": self.batchnorm2.weight.grad.numpy(),
            "dLdConv2_W": np.moveaxis(self.conv2.weight.grad.numpy(), orig, W_swap),
            "dLdConv2_b": self.conv2.bias.grad.numpy().reshape(1, 1, 1, -1),
            "dLdBn1_intercept": self.batchnorm1.bias.grad.numpy(),
            "dLdBn1_scaler": self.batchnorm1.weight.grad.numpy(),
            "dLdConv1_W": np.moveaxis(self.conv1.weight.grad.numpy(), orig, W_swap),
            "dLdConv1_b": self.conv1.bias.grad.numpy().reshape(1, 1, 1, -1),
        }
        return grads


class TorchBidirectionalLSTM(nn.Module):
    def __init__(self, n_in, n_out, params, **kwargs):
        super(TorchBidirectionalLSTM, self).__init__()

        self.layer1 = nn.LSTM(
            input_size=n_in,
            hidden_size=n_out,
            num_layers=1,
            bidirectional=True,
            bias=True,
        )

        Wiu = params["components"]["forward"]["Wu"][n_out:, :].T
        Wif = params["components"]["forward"]["Wf"][n_out:, :].T
        Wic = params["components"]["forward"]["Wc"][n_out:, :].T
        Wio = params["components"]["forward"]["Wo"][n_out:, :].T
        W_ih_f = np.vstack([Wiu, Wif, Wic, Wio])

        Whu = params["components"]["forward"]["Wu"][:n_out, :].T
        Whf = params["components"]["forward"]["Wf"][:n_out, :].T
        Whc = params["components"]["forward"]["Wc"][:n_out, :].T
        Who = params["components"]["forward"]["Wo"][:n_out, :].T
        W_hh_f = np.vstack([Whu, Whf, Whc, Who])

        assert self.layer1.weight_ih_l0.shape == W_ih_f.shape
        assert self.layer1.weight_hh_l0.shape == W_hh_f.shape

        self.layer1.weight_ih_l0 = nn.Parameter(torch.FloatTensor(W_ih_f))
        self.layer1.weight_hh_l0 = nn.Parameter(torch.FloatTensor(W_hh_f))

        Wiu = params["components"]["backward"]["Wu"][n_out:, :].T
        Wif = params["components"]["backward"]["Wf"][n_out:, :].T
        Wic = params["components"]["backward"]["Wc"][n_out:, :].T
        Wio = params["components"]["backward"]["Wo"][n_out:, :].T
        W_ih_b = np.vstack([Wiu, Wif, Wic, Wio])

        Whu = params["components"]["backward"]["Wu"][:n_out, :].T
        Whf = params["components"]["backward"]["Wf"][:n_out, :].T
        Whc = params["components"]["backward"]["Wc"][:n_out, :].T
        Who = params["components"]["backward"]["Wo"][:n_out, :].T
        W_hh_b = np.vstack([Whu, Whf, Whc, Who])

        assert self.layer1.weight_ih_l0_reverse.shape == W_ih_b.shape
        assert self.layer1.weight_hh_l0_reverse.shape == W_hh_b.shape

        self.layer1.weight_ih_l0_reverse = nn.Parameter(torch.FloatTensor(W_ih_b))
        self.layer1.weight_hh_l0_reverse = nn.Parameter(torch.FloatTensor(W_hh_b))

        b_f = np.concatenate(
            [
                params["components"]["forward"]["bu"],
                params["components"]["forward"]["bf"],
                params["components"]["forward"]["bc"],
                params["components"]["forward"]["bo"],
            ],
            axis=-1,
        ).flatten()

        assert self.layer1.bias_ih_l0.shape == b_f.shape
        assert self.layer1.bias_hh_l0.shape == b_f.shape

        self.layer1.bias_ih_l0 = nn.Parameter(torch.FloatTensor(b_f))
        self.layer1.bias_hh_l0 = nn.Parameter(torch.FloatTensor(b_f))

        b_b = np.concatenate(
            [
                params["components"]["backward"]["bu"],
                params["components"]["backward"]["bf"],
                params["components"]["backward"]["bc"],
                params["components"]["backward"]["bo"],
            ],
            axis=-1,
        ).flatten()

        assert self.layer1.bias_ih_l0_reverse.shape == b_b.shape
        assert self.layer1.bias_hh_l0_reverse.shape == b_b.shape

        self.layer1.bias_ih_l0_reverse = nn.Parameter(torch.FloatTensor(b_b))
        self.layer1.bias_hh_l0_reverse = nn.Parameter(torch.FloatTensor(b_b))

    def forward(self, X):
        # (batch, input_size, seq_len) -> (seq_len, batch, input_size)
        self.X = np.moveaxis(X, [0, 1, 2], [-2, -1, -3])

        if not isinstance(self.X, torch.Tensor):
            self.X = torchify(self.X)

        self.X.retain_grad()

        # initial hidden state is 0
        n_ex, n_in, n_timesteps = self.X.shape
        n_out, n_out = self.layer1.weight_hh_l0.shape

        # forward pass
        self.A, (At, Ct) = self.layer1(self.X)
        self.A.retain_grad()
        return self.A

    def extract_grads(self, X):
        self.forward(X)
        self.loss = self.A.sum()
        self.loss.backward()

        # forward
        w_ii, w_if, w_ic, w_io = self.layer1.weight_ih_l0.chunk(4, 0)
        w_hi, w_hf, w_hc, w_ho = self.layer1.weight_hh_l0.chunk(4, 0)
        bu_f, bf_f, bc_f, bo_f = self.layer1.bias_ih_l0.chunk(4, 0)

        Wu_f = torch.cat([torch.t(w_hi), torch.t(w_ii)], dim=0)
        Wf_f = torch.cat([torch.t(w_hf), torch.t(w_if)], dim=0)
        Wc_f = torch.cat([torch.t(w_hc), torch.t(w_ic)], dim=0)
        Wo_f = torch.cat([torch.t(w_ho), torch.t(w_io)], dim=0)

        dw_ii, dw_if, dw_ic, dw_io = self.layer1.weight_ih_l0.grad.chunk(4, 0)
        dw_hi, dw_hf, dw_hc, dw_ho = self.layer1.weight_hh_l0.grad.chunk(4, 0)
        dbu_f, dbf_f, dbc_f, dbo_f = self.layer1.bias_ih_l0.grad.chunk(4, 0)

        dWu_f = torch.cat([torch.t(dw_hi), torch.t(dw_ii)], dim=0)
        dWf_f = torch.cat([torch.t(dw_hf), torch.t(dw_if)], dim=0)
        dWc_f = torch.cat([torch.t(dw_hc), torch.t(dw_ic)], dim=0)
        dWo_f = torch.cat([torch.t(dw_ho), torch.t(dw_io)], dim=0)

        # backward
        w_ii, w_if, w_ic, w_io = self.layer1.weight_ih_l0_reverse.chunk(4, 0)
        w_hi, w_hf, w_hc, w_ho = self.layer1.weight_hh_l0_reverse.chunk(4, 0)
        bu_b, bf_b, bc_b, bo_b = self.layer1.bias_ih_l0_reverse.chunk(4, 0)

        Wu_b = torch.cat([torch.t(w_hi), torch.t(w_ii)], dim=0)
        Wf_b = torch.cat([torch.t(w_hf), torch.t(w_if)], dim=0)
        Wc_b = torch.cat([torch.t(w_hc), torch.t(w_ic)], dim=0)
        Wo_b = torch.cat([torch.t(w_ho), torch.t(w_io)], dim=0)

        dw_ii, dw_if, dw_ic, dw_io = self.layer1.weight_ih_l0_reverse.grad.chunk(4, 0)
        dw_hi, dw_hf, dw_hc, dw_ho = self.layer1.weight_hh_l0_reverse.grad.chunk(4, 0)
        dbu_b, dbf_b, dbc_b, dbo_b = self.layer1.bias_ih_l0_reverse.grad.chunk(4, 0)

        dWu_b = torch.cat([torch.t(dw_hi), torch.t(dw_ii)], dim=0)
        dWf_b = torch.cat([torch.t(dw_hf), torch.t(dw_if)], dim=0)
        dWc_b = torch.cat([torch.t(dw_hc), torch.t(dw_ic)], dim=0)
        dWo_b = torch.cat([torch.t(dw_ho), torch.t(dw_io)], dim=0)

        orig, X_swap = [0, 1, 2], [-1, -3, -2]
        grads = {
            "X": np.moveaxis(self.X.detach().numpy(), orig, X_swap),
            "Wu_f": Wu_f.detach().numpy(),
            "Wf_f": Wf_f.detach().numpy(),
            "Wc_f": Wc_f.detach().numpy(),
            "Wo_f": Wo_f.detach().numpy(),
            "bu_f": bu_f.detach().numpy().reshape(-1, 1),
            "bf_f": bf_f.detach().numpy().reshape(-1, 1),
            "bc_f": bc_f.detach().numpy().reshape(-1, 1),
            "bo_f": bo_f.detach().numpy().reshape(-1, 1),
            "Wu_b": Wu_b.detach().numpy(),
            "Wf_b": Wf_b.detach().numpy(),
            "Wc_b": Wc_b.detach().numpy(),
            "Wo_b": Wo_b.detach().numpy(),
            "bu_b": bu_b.detach().numpy().reshape(-1, 1),
            "bf_b": bf_b.detach().numpy().reshape(-1, 1),
            "bc_b": bc_b.detach().numpy().reshape(-1, 1),
            "bo_b": bo_b.detach().numpy().reshape(-1, 1),
            "y": np.moveaxis(self.A.detach().numpy(), orig, X_swap),
            "dLdA": self.A.grad.numpy(),
            "dLdWu_f": dWu_f.numpy(),
            "dLdWf_f": dWf_f.numpy(),
            "dLdWc_f": dWc_f.numpy(),
            "dLdWo_f": dWo_f.numpy(),
            "dLdBu_f": dbu_f.numpy().reshape(-1, 1),
            "dLdBf_f": dbf_f.numpy().reshape(-1, 1),
            "dLdBc_f": dbc_f.numpy().reshape(-1, 1),
            "dLdBo_f": dbo_f.numpy().reshape(-1, 1),
            "dLdWu_b": dWu_b.numpy(),
            "dLdWf_b": dWf_b.numpy(),
            "dLdWc_b": dWc_b.numpy(),
            "dLdWo_b": dWo_b.numpy(),
            "dLdBu_b": dbu_b.numpy().reshape(-1, 1),
            "dLdBf_b": dbf_b.numpy().reshape(-1, 1),
            "dLdBc_b": dbc_b.numpy().reshape(-1, 1),
            "dLdBo_b": dbo_b.numpy().reshape(-1, 1),
            "dLdX": np.moveaxis(self.X.grad.numpy(), orig, X_swap),
        }
        return grads


class TorchPool2DLayer(nn.Module):
    def __init__(self, in_channels, hparams, **kwargs):
        super(TorchPool2DLayer, self).__init__()

        if hparams["mode"] == "max":
            self.layer1 = nn.MaxPool2d(
                kernel_size=hparams["kernel_shape"],
                padding=hparams["pad"],
                stride=hparams["stride"],
            )
        elif hparams["mode"] == "average":
            self.layer1 = nn.AvgPool2d(
                kernel_size=hparams["kernel_shape"],
                padding=hparams["pad"],
                stride=hparams["stride"],
            )

    def forward(self, X):
        # (N, H, W, C) -> (N, C, H, W)
        self.X = np.moveaxis(X, [0, 1, 2, 3], [0, -2, -1, -3])
        if not isinstance(self.X, torch.Tensor):
            self.X = torchify(self.X)

        self.X.retain_grad()
        self.Y = self.layer1(self.X)
        self.Y.retain_grad()
        return self.Y

    def extract_grads(self, X):
        self.forward(X)
        self.loss = self.Y.sum()
        self.loss.backward()

        # W (theirs): (n_out, n_in, f[0], f[1]) -> W (mine): (f[0], f[1], n_in, n_out)
        # X (theirs): (N, C, H, W)              -> X (mine): (N, H, W, C)
        # Y (theirs): (N, C, H, W)              -> Y (mine): (N, H, W, C)
        orig, X_swap = [0, 1, 2, 3], [0, -1, -3, -2]
        grads = {
            "X": np.moveaxis(self.X.detach().numpy(), orig, X_swap),
            "y": np.moveaxis(self.Y.detach().numpy(), orig, X_swap),
            "dLdY": np.moveaxis(self.Y.grad.numpy(), orig, X_swap),
            "dLdX": np.moveaxis(self.X.grad.numpy(), orig, X_swap),
        }
        return grads


class TorchConv2DLayer(nn.Module):
    def __init__(self, in_channels, out_channels, act_fn, params, hparams, **kwargs):
        super(TorchConv2DLayer, self).__init__()

        W = params["W"]
        b = params["b"]
        self.act_fn = act_fn

        self.layer1 = nn.Conv2d(
            in_channels,
            out_channels,
            hparams["kernel_shape"],
            padding=hparams["pad"],
            stride=hparams["stride"],
            bias=True,
        )

        # (f[0], f[1], n_in, n_out) -> (n_out, n_in, f[0], f[1])
        W = np.moveaxis(W, [0, 1, 2, 3], [-2, -1, -3, -4])
        assert self.layer1.weight.shape == W.shape
        assert self.layer1.bias.shape == b.flatten().shape

        self.layer1.weight = nn.Parameter(torch.FloatTensor(W))
        self.layer1.bias = nn.Parameter(torch.FloatTensor(b.flatten()))

    def forward(self, X):
        # (N, H, W, C) -> (N, C, H, W)
        self.X = np.moveaxis(X, [0, 1, 2, 3], [0, -2, -1, -3])
        if not isinstance(self.X, torch.Tensor):
            self.X = torchify(self.X)

        self.X.retain_grad()

        self.Z = self.layer1(self.X)
        self.Z.retain_grad()

        self.Y = self.act_fn(self.Z)
        self.Y.retain_grad()
        return self.Y

    def extract_grads(self, X):
        self.forward(X)
        self.loss = self.Y.sum()
        self.loss.backward()

        # W (theirs): (n_out, n_in, f[0], f[1]) -> W (mine): (f[0], f[1], n_in, n_out)
        # X (theirs): (N, C, H, W)              -> X (mine): (N, H, W, C)
        # Y (theirs): (N, C, H, W)              -> Y (mine): (N, H, W, C)
        orig, X_swap, W_swap = [0, 1, 2, 3], [0, -1, -3, -2], [-1, -2, -4, -3]
        grads = {
            "X": np.moveaxis(self.X.detach().numpy(), orig, X_swap),
            "W": np.moveaxis(self.layer1.weight.detach().numpy(), orig, W_swap),
            "b": self.layer1.bias.detach().numpy().reshape(1, 1, 1, -1),
            "y": np.moveaxis(self.Y.detach().numpy(), orig, X_swap),
            "dLdY": np.moveaxis(self.Y.grad.numpy(), orig, X_swap),
            "dLdZ": np.moveaxis(self.Z.grad.numpy(), orig, X_swap),
            "dLdW": np.moveaxis(self.layer1.weight.grad.numpy(), orig, W_swap),
            "dLdB": self.layer1.bias.grad.numpy().reshape(1, 1, 1, -1),
            "dLdX": np.moveaxis(self.X.grad.numpy(), orig, X_swap),
        }
        return grads


class TorchLSTMCell(nn.Module):
    def __init__(self, n_in, n_out, params, **kwargs):
        super(TorchLSTMCell, self).__init__()

        Wiu = params["Wu"][n_out:, :].T
        Wif = params["Wf"][n_out:, :].T
        Wic = params["Wc"][n_out:, :].T
        Wio = params["Wo"][n_out:, :].T
        W_ih = np.vstack([Wiu, Wif, Wic, Wio])

        Whu = params["Wu"][:n_out, :].T
        Whf = params["Wf"][:n_out, :].T
        Whc = params["Wc"][:n_out, :].T
        Who = params["Wo"][:n_out, :].T
        W_hh = np.vstack([Whu, Whf, Whc, Who])

        self.layer1 = nn.LSTMCell(input_size=n_in, hidden_size=n_out, bias=True)
        assert self.layer1.weight_ih.shape == W_ih.shape
        assert self.layer1.weight_hh.shape == W_hh.shape
        self.layer1.weight_ih = nn.Parameter(torch.FloatTensor(W_ih))
        self.layer1.weight_hh = nn.Parameter(torch.FloatTensor(W_hh))

        b = np.concatenate(
            [params["bu"], params["bf"], params["bc"], params["bo"]], axis=-1
        ).flatten()
        assert self.layer1.bias_ih.shape == b.shape
        assert self.layer1.bias_hh.shape == b.shape
        self.layer1.bias_ih = nn.Parameter(torch.FloatTensor(b))
        self.layer1.bias_hh = nn.Parameter(torch.FloatTensor(b))

    def forward(self, X):
        self.X = X
        if not isinstance(self.X, torch.Tensor):
            self.X = torchify(self.X)

        self.X.retain_grad()

        # initial hidden state is 0
        n_ex, n_in, n_timesteps = self.X.shape
        n_out, n_out = self.layer1.weight_hh.shape

        # initialize hidden states
        a0 = torchify(np.zeros((n_ex, n_out)))
        c0 = torchify(np.zeros((n_ex, n_out)))
        a0.retain_grad()
        c0.retain_grad()

        # forward pass
        A, C = [], []
        at = a0
        ct = c0
        for t in range(n_timesteps):
            A.append(at)
            C.append(ct)
            at1, ct1 = self.layer1(self.X[:, :, t], (at, ct))
            at.retain_grad()
            ct.retain_grad()
            at = at1
            ct = ct1

        at.retain_grad()
        ct.retain_grad()
        A.append(at)
        C.append(ct)

        # don't inclue a0 in our outputs
        self.A = A[1:]
        self.C = C[1:]
        return self.A, self.C

    def extract_grads(self, X):
        self.forward(X)
        self.loss = torch.stack(self.A).sum()
        self.loss.backward()

        w_ii, w_if, w_ic, w_io = self.layer1.weight_ih.chunk(4, 0)
        w_hi, w_hf, w_hc, w_ho = self.layer1.weight_hh.chunk(4, 0)
        bu, bf, bc, bo = self.layer1.bias_ih.chunk(4, 0)

        Wu = torch.cat([torch.t(w_hi), torch.t(w_ii)], dim=0)
        Wf = torch.cat([torch.t(w_hf), torch.t(w_if)], dim=0)
        Wc = torch.cat([torch.t(w_hc), torch.t(w_ic)], dim=0)
        Wo = torch.cat([torch.t(w_ho), torch.t(w_io)], dim=0)

        dw_ii, dw_if, dw_ic, dw_io = self.layer1.weight_ih.grad.chunk(4, 0)
        dw_hi, dw_hf, dw_hc, dw_ho = self.layer1.weight_hh.grad.chunk(4, 0)
        dbu, dbf, dbc, dbo = self.layer1.bias_ih.grad.chunk(4, 0)

        dWu = torch.cat([torch.t(dw_hi), torch.t(dw_ii)], dim=0)
        dWf = torch.cat([torch.t(dw_hf), torch.t(dw_if)], dim=0)
        dWc = torch.cat([torch.t(dw_hc), torch.t(dw_ic)], dim=0)
        dWo = torch.cat([torch.t(dw_ho), torch.t(dw_io)], dim=0)

        grads = {
            "X": self.X.detach().numpy(),
            "Wu": Wu.detach().numpy(),
            "Wf": Wf.detach().numpy(),
            "Wc": Wc.detach().numpy(),
            "Wo": Wo.detach().numpy(),
            "bu": bu.detach().numpy().reshape(-1, 1),
            "bf": bf.detach().numpy().reshape(-1, 1),
            "bc": bc.detach().numpy().reshape(-1, 1),
            "bo": bo.detach().numpy().reshape(-1, 1),
            "C": torch.stack(self.C).detach().numpy(),
            "y": np.swapaxes(
                np.swapaxes(torch.stack(self.A).detach().numpy(), 1, 0), 1, 2
            ),
            "dLdA": np.array([a.grad.numpy() for a in self.A]),
            "dLdWu": dWu.numpy(),
            "dLdWf": dWf.numpy(),
            "dLdWc": dWc.numpy(),
            "dLdWo": dWo.numpy(),
            "dLdBu": dbu.numpy().reshape(-1, 1),
            "dLdBf": dbf.numpy().reshape(-1, 1),
            "dLdBc": dbc.numpy().reshape(-1, 1),
            "dLdBo": dbo.numpy().reshape(-1, 1),
            "dLdX": self.X.grad.numpy(),
        }
        return grads


class TorchRNNCell(nn.Module):
    def __init__(self, n_in, n_hid, params, **kwargs):
        super(TorchRNNCell, self).__init__()

        self.layer1 = nn.RNNCell(n_in, n_hid, bias=True, nonlinearity="tanh")

        # set weights and bias to match those of RNNCell
        # NB: we pass the *transpose* of the RNNCell weights and biases to
        # pytorch, meaning wee need to check against the *transpose* of our
        # outputs for any function of the weights
        self.layer1.weight_ih = nn.Parameter(torch.FloatTensor(params["Wax"].T))
        self.layer1.weight_hh = nn.Parameter(torch.FloatTensor(params["Waa"].T))
        self.layer1.bias_ih = nn.Parameter(torch.FloatTensor(params["bx"].T))
        self.layer1.bias_hh = nn.Parameter(torch.FloatTensor(params["ba"].T))

    def forward(self, X):
        self.X = X
        if not isinstance(self.X, torch.Tensor):
            self.X = torchify(self.X)

        self.X.retain_grad()

        # initial hidden state is 0
        n_ex, n_in, n_timesteps = self.X.shape
        n_out, n_out = self.layer1.weight_hh.shape

        # initialize hidden states
        a0 = torchify(np.zeros((n_ex, n_out)))
        a0.retain_grad()

        # forward pass
        A = []
        at = a0
        for t in range(n_timesteps):
            A += [at]
            at1 = self.layer1(self.X[:, :, t], at)
            at.retain_grad()
            at = at1

        at.retain_grad()
        A += [at]

        # don't inclue a0 in our outputs
        self.A = A[1:]
        return self.A

    def extract_grads(self, X):
        self.forward(X)
        self.loss = torch.stack(self.A).sum()
        self.loss.backward()
        grads = {
            "X": self.X.detach().numpy(),
            "ba": self.layer1.bias_hh.detach().numpy(),
            "bx": self.layer1.bias_ih.detach().numpy(),
            "Wax": self.layer1.weight_ih.detach().numpy(),
            "Waa": self.layer1.weight_hh.detach().numpy(),
            "y": torch.stack(self.A).detach().numpy(),
            "dLdA": np.array([a.grad.numpy() for a in self.A]),
            "dLdWaa": self.layer1.weight_hh.grad.numpy(),
            "dLdWax": self.layer1.weight_ih.grad.numpy(),
            "dLdBa": self.layer1.bias_hh.grad.numpy(),
            "dLdBx": self.layer1.bias_ih.grad.numpy(),
            "dLdX": self.X.grad.numpy(),
        }
        return grads


class TorchFCLayer(nn.Module):
    def __init__(self, n_in, n_hid, act_fn, params, **kwargs):
        super(TorchFCLayer, self).__init__()
        self.layer1 = nn.Linear(n_in, n_hid)

        # explicitly set weights and bias
        # NB: we pass the *transpose* of the weights to pytorch, meaning
        # we'll need to check against the *transpose* of our outputs for
        # any function of the weights
        self.layer1.weight = nn.Parameter(torch.FloatTensor(params["W"].T))
        self.layer1.bias = nn.Parameter(torch.FloatTensor(params["b"]))

        self.act_fn = act_fn
        self.model = nn.Sequential(self.layer1, self.act_fn)

    def forward(self, X):
        self.X = X
        if not isinstance(X, torch.Tensor):
            self.X = torchify(X)

        self.z1 = self.layer1(self.X)
        self.z1.retain_grad()

        self.out1 = self.act_fn(self.z1)
        self.out1.retain_grad()

    def extract_grads(self, X):
        self.forward(X)
        self.loss1 = self.out1.sum()
        self.loss1.backward()
        grads = {
            "X": self.X.detach().numpy(),
            "b": self.layer1.bias.detach().numpy(),
            "W": self.layer1.weight.detach().numpy(),
            "y": self.out1.detach().numpy(),
            "dLdy": self.out1.grad.numpy(),
            "dLdZ": self.z1.grad.numpy(),
            "dLdB": self.layer1.bias.grad.numpy(),
            "dLdW": self.layer1.weight.grad.numpy(),
            "dLdX": self.X.grad.numpy(),
        }
        return grads
