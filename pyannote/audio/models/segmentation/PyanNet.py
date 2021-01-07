# MIT License
#
# Copyright (c) 2020 CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange

from pyannote.audio.core.model import Model
from pyannote.audio.core.task import Task
from pyannote.audio.models.blocks.binarize import Binarize
from pyannote.audio.models.blocks.sincnet import SincNet
from pyannote.core.utils.generators import pairwise


class PyanNet(Model):
    """PyanNet segmentation model

    SincNet > LSTM > Feed forward > Classifier

    Parameters
    ----------
    sample_rate : int, optional
        Audio sample rate. Defaults to 16kHz (16000).
    num_channels : int, optional
        Number of channels. Defaults to mono (1).
    sincnet : dict, optional
        Keyword arugments passed to the SincNet block.
        Defaults to {"stride": 1}.
    lstm : dict, optional
        Keyword arguments passed to the LSTM layer.
        Defaults to {"hidden_size": 128, "num_layers": 2, "bidirectional": True},
        i.e. two bidirectional layers with 128 units each.
    linear : dict, optional
        Keyword arugments used to initialize linear layers
        Defaults to {"hidden_size": 128, "num_layers": 2},
        i.e. two linear layers with 128 units each.
    """

    SINCNET_DEFAULTS = {"stride": 1}
    LSTM_DEFAULTS = {"hidden_size": 128, "num_layers": 2, "bidirectional": True}
    LINEAR_DEFAULTS = {"hidden_size": 128, "num_layers": 2}

    def __init__(
        self,
        sample_rate: int = 16000,
        num_channels: int = 1,
        sincnet: dict = None,
        lstm: dict = None,
        linear: dict = None,
        task: Optional[Task] = None,
    ):

        super().__init__(sample_rate=sample_rate, num_channels=num_channels, task=task)

        sincnet_hparams = dict(**self.SINCNET_DEFAULTS)
        if sincnet is not None:
            sincnet_hparams.update(**sincnet)
        sincnet_hparams["sample_rate"] = sample_rate
        self.hparams.sincnet = sincnet_hparams
        self.sincnet = SincNet(**self.hparams.sincnet)

        lstm_hparams = dict(**self.LSTM_DEFAULTS)
        if lstm is not None:
            lstm_hparams.update(**lstm)
        lstm_hparams["batch_first"] = True  # this is not negotiable
        self.hparams.lstm = lstm_hparams
        self.lstm = nn.LSTM(60, **self.hparams.lstm)

        lstm_out_features: int = self.lstm.hidden_size * (
            2 if self.lstm.bidirectional else 1
        )

        linear_hparams = dict(**self.LINEAR_DEFAULTS)
        if linear is not None:
            linear_hparams.update(**linear)
        self.hparams.linear = linear_hparams
        if self.hparams.linear["num_layers"] > 0:
            self.linear = nn.ModuleList(
                [
                    nn.Linear(in_features, out_features)
                    for in_features, out_features in pairwise(
                        [
                            lstm_out_features,
                        ]
                        + [self.hparams.linear["hidden_size"]]
                        * self.hparams.linear["num_layers"]
                    )
                ]
            )

    def build(self):

        if self.hparams.linear["num_layers"] > 0:
            in_features = self.hparams.linear["hidden_size"]
        else:
            in_features = self.lstm.hidden_size * (2 if self.lstm.bidirectional else 1)

        self.classifier = nn.Linear(
            in_features, len(self.hparams.task_specifications.classes)
        )
        self.activation = self.default_activation()

    def forward(self, waveforms: torch.Tensor) -> torch.Tensor:
        """Pass forward

        Parameters
        ----------
        waveforms : (batch, channel, sample)

        Returns
        -------
        scores : (batch, frame, classes)
        """

        outputs = self.sincnet(waveforms)

        outputs, _ = self.lstm(
            rearrange(outputs, "batch feature frame -> batch frame feature")
        )

        if self.hparams.linear["num_layers"] > 0:
            for linear in self.linear:
                outputs = F.leaky_relu(linear(outputs))

        return self.activation(self.classifier(outputs))


class BinaryPyanNet(PyanNet):
    def build(self):
        super().build()
        self.binarize = Binarize(len(self.hparams.task_specifications.classes))

    def forward(self, waveforms: torch.Tensor) -> torch.Tensor:
        return self.binarize(super().forward(waveforms))