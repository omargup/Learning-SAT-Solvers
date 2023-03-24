from src.solvers import pg_solver
import os

from ray import tune, air
from ray.tune.schedulers import ASHAScheduler
from ray.tune.search import ConcurrencyLimiter
from ray.tune.search.optuna import OptunaSearch
import optuna

############################################
# This example runs a conditional hyperparameters search
# with optuna and raytune.
############################################

def define_by_run_func(trial):
    # Conditional search space
    decoder = trial.suggest_categorical("decoder", ['GRU', 'LSTM', 'Transformer'])
    if (decoder == 'GRU') or (decoder == 'LSTM'):
        trial.suggest_int("hidden_size", 64, 1024, log=True)
        trial.suggest_categorical("trainable_state", [True, False])
    else:  # Transformer
        trial.suggest_int("num_heads", 1, 8)
        trial.suggest_int("dense_size", 64, 1024, log=True)
    
    # Search space
    trial.suggest_categorical("batch_size", [2, 4, 8, 16, 32, 64])
    trial.suggest_float("lr", 1e-6, 1e-4, log=True)
    trial.suggest_categorical("k_samples", [2, 4, 8, 16, 32])
    trial.suggest_categorical("entropy_estimator", ['crude', 'smooth'])
    trial.suggest_categorical("beta_entropy", [0.01, 0.02, 0.03])
    
    # Constants
    config = {
    # Encoder
    "node2vec": True,  # {False, True}
    "n2v_dir": os.path.abspath("n2v_emb"), 
    "n2v_dim": 64,
    "n2v_pretrained": True,  # {False, True}
    "n2v_walk_len": 10,
    "n2v_context_size": 5,
    "n2v_walks_per_node": 5,
    "n2v_p": 1,
    "n2v_q": 1,
    "n2v_batch_size": 32,
    "n2v_lr": 0.01,
    "n2v_num_epochs": 100,
    "n2v_workers": 0,  # {0, 1, 2, 3, 4}
    "n2v_verbose": 1,  # {0, 1, 2}

    # Initializers
    "dec_var_initializer": "Node2VecVar",  # {"BasicVar", "Node2VecVar"}
    "dec_context_initializer": "Node2VecContext",  # {"EmptyContext", "Node2VecContext"}

    # Embeddings
    "var_emb_size": 128,
    "assignment_emb_size": 64,
    "context_emb_size": 128,
    "model_dim": 128, 

    # Architecture
    #"decoder": 'GRU',  # {'GRU', 'LSTM', "Transformer"}
    "num_layers": 2,  
    "output_size": 2,  # Decoder output size: 1, 2
    "dropout": 0,

    #"hidden_size": 128,  # Useful for RNN
    #"trainable_state": True,  # {False, True}. Useful for RNN

    #"num_heads": 2,  # Useful for Transformer
    #"dense_size":128,  # Useful for Transformer

    # Training
    "num_samples": 15000,  #4000
    "accumulation_episodes": 1,
    #"batch_size": 10,  #10
    "permute_vars": True,
    "permute_seed": None,  # 2147483647
    "clip_grad": 1,  # {None, float}
    #"lr": 0.00001,  # 0.00015   0.00001

    # Baseline
    "baseline": 'sample',  # {None, 'greedy', 'sample'. 'ema'}
    "alpha_ema": 0.99,  # 0 <= alpha <= 1. EMA decay, useful if baseline == 'ema'
    #"k_samples": 10,  # int, k >= 1. Number of samples used to obtain the baseline value, useful if baseline == 'sample'

    # Exploration
    "logit_clipping": None,  # {None, int >= 1}
    "logit_temp": None,  # {None, float >= 1}. Useful for improve exploration in evaluation.
    #"entropy_estimator": 'crude',  # {'crude', 'smooth'}
    #"beta_entropy": 0.03,  # float, beta >= 0.

    # Misc
    "sat_stopping": False,  # {True, False}. Stop when num_sat is equal with the num of clauses.
    "log_interval": 20,
    "eval_interval": 100,
    "eval_strategies": [0, 32],
    "tensorboard_on": True,
    "extra_logging": False,  # log TrainableState's weights
    "raytune": True,
    "data_dir": os.path.abspath('data/sat_rand/sat_rand_n=0020_k=03_m=0080_i=1.cnf'),
    "verbose": 0,  # {0, 1, 2}. If raytune is True, then verbose is set to 0.

    "log_dir": 'logs',
    "output_dir": 'outputs',
    "exp_name": 'exp_tune2',
    "run_name": 'run',
    "gpu": True,
    "checkpoint_dir": None} 

    # Optuna defined by run flag. Don't set to False
    config["optuna_by_run"] = True

    return config
    

search_alg = OptunaSearch(sampler=optuna.samplers.TPESampler(multivariate=False),
                          space=define_by_run_func,
                          mode='max',
                          metric="num_sat_sample_32",)
search_alg = ConcurrencyLimiter(search_alg, max_concurrent=4)
scheduler = ASHAScheduler(grace_period=5)
tune_config = tune.TuneConfig(mode='max',
                              metric="num_sat_sample_32",
                              num_samples=5,
                              search_alg=search_alg,
                              scheduler=scheduler)
run_config = air.RunConfig(local_dir="experiments",
                           name="exp_tune2",
                           progress_reporter=None,
                           log_to_file=True)

tuner = tune.Tuner(pg_solver,
                   tune_config=tune_config,
                   run_config=run_config)

results = tuner.fit()