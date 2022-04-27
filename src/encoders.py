import torch
import torch.nn as nn
import torch.nn.functional as F

from src.embeddings import BaseEmbedding
from typing import List, Optional, Tuple, Union
from utils import build_gcn_model

class Encoder(nn.Module):
    """ """
    def __init__(self, **kwargs):
        super(Encoder, self).__init__(**kwargs)

    def forward(self, X, *args):
        raise NotImplementedError


class RNNEncoder(Encoder):
    """ """
    def __init__(self, cell='GRU', embedding=None, embedding_size=128, hidden_size=128,
                    num_layers=1, dropout=0, **kwargs):
        super(RNNEncoder, self).__init__(**kwargs)

        # Embedding
        self.embedding = embedding
        if embedding is None:
            raise TypeError("An embedding must be specified.")
        elif embedding is not None:
            if not issubclass(type(self.embedding), BaseEmbedding):
                raise TypeError("embedding must inherit from BaseEmbedding.")
        
        # RNN
        if cell == 'GRU':
            self.rnn = nn.GRU(embedding_size, hidden_size, num_layers, dropout = dropout)
        elif cell == 'LSTM':
            self.rnn = nn.LSTM(embedding_size, hidden_size, num_layers, dropout = dropout)
        else:
            raise TypeError("{} is not a valid cell, try with 'LSTM' or 'GRU'.".format(self.cell))

    def forward(self, formula, *args):
        # ::formula:: [batch_size, seq_len, features_size]
        X = self.embedding(formula)
        # X shape: [batch_size, seq_len, embedding_size]
        X = X.permute(1, 0, 2)
        # X shape: [seq_len, batch_size, embedding_size]
        output, state = self.rnn(X)  # Initial state is zeros
        # output shape: [seq_len, batch_size, hidden_size]
        # state shape: [num_layers, batch_size, hidden_size]
        return output, state

class GCNEncoder(Encoder):
    def __init__(self,
        embedding_size: int,
        hidden_sizes: List[int]=[16],
        intermediate_fns: Optional[List[List[Union[nn.Module, None]]]]=[
            [None],
            [nn.ReLU(), nn.Dropout(p=0.2)], 
            [nn.ReLU()]
        ],
        node_types: List[str] = ["literal", "clause"],
        edge_types: List[Tuple[str, str, str]]=[
            ("literal", "exists_in", "clause"),
            ("clause", "contains", "literal"),
        ],
        **kwargs
    ):

        super(GCNEncoder, self).__init__(**kwargs)

        self.module_ = build_gcn_model(
            embedding_size,
            hidden_sizes=hidden_sizes,
            intermediate_fns=intermediate_fns,
            node_types=node_types,
            edge_types=edge_types
        )

    def forward(self, x, edge_index):    
        return self.module_(x, edge_index)