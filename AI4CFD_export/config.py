"""
Configuration for the AI4CFD simulation pipeline.
Application -> Geometric AI -> CFD -> Deep Learning -> Prediction -> Optimization
"""

import os

# --- Module flag ---
MODULE = "train"  # MODULE = "train" or "test"
# Control the dataset_creation_{geo}
REQUIRED_DATASET = False  # run simulations (step 1); False = skip to post-processing
REQUIRED_VAE     = False # True  → VAE runner (varied geometry from latent space)
                         # False → original batch runner (fixed or lightly randomised geometry)
GEO_FIXED        = True  # meaningful only when REQUIRED_VAE = False
                         # True  → use the fixed-geometry batch runner (e.g. ConicalTank with fixed dims)
                         # False → use the randomised batch runner (uniform random params, no VAE)
# --- Geometry ---
GEO_TYPE = "pipe"         # "pipe" or "tank" or "airfoil"
GEO_NAME = "bendpipe"  # "bendpipe" or "twobendpipe" or "contactTank" or "airfoil"

# -----------------------------OPENFOAM --------------------------------------
# ----------------------------CHANGE FOR CASE --------------------------------
# --- Target ---
# What data you want to train and predict
# "Boundary": only the 3D boundary layer region near the wall (most relevant for engineering)
# "Full-Field": the entire flow domain (more general but harder to learn)
TARGET_RESULT_FIELD = "Full-Field"  # "Boundary" or "Full-Field"
# if TARGET_RESULT_FIELD = "Boundary", then select which boundary for training
TARGET_RESULT_NAME = "airfoil"  # "airfoil" or "pipe" or "tank"

# --- Gradient fields ---
# True  → grad(U)/grad(p) must be listed in TRAIN_FIELDS; they are added to the
#         model input as extra per-point features (see AUX_INPUT_FIELDS).
# False → grad(U)/grad(p) must NOT be listed in TRAIN_FIELDS.
# Mismatches between USE_GRADIENT and TRAIN_FIELDS raise an error below.
USE_GRADIENT    = False
GRADIENT_FIELDS = ["grad(U)", "grad(p)"]

# --- Fields to train and predict ---
TRAIN_FIELDS   = ["U", "p"]  # "k", "omega", "p", "p_rgh", "alpha.water"
PREDICT_FIELDS = ["U", "p"]  # "k", "omega", "p", "p_rgh", "alpha.water"

# --- Validate USE_GRADIENT / TRAIN_FIELDS consistency ---
_has_gradient_fields = any(f in TRAIN_FIELDS for f in GRADIENT_FIELDS)
if USE_GRADIENT and not all(f in TRAIN_FIELDS for f in GRADIENT_FIELDS):
    raise ValueError(
        f"USE_GRADIENT=True requires {GRADIENT_FIELDS} in TRAIN_FIELDS, "
        f"but TRAIN_FIELDS={TRAIN_FIELDS}."
    )
if not USE_GRADIENT and _has_gradient_fields:
    raise ValueError(
        f"USE_GRADIENT=False but TRAIN_FIELDS={TRAIN_FIELDS} contains gradient "
        f"fields {GRADIENT_FIELDS}. Remove them or set USE_GRADIENT=True."
    )

# --- Wall Distance ---
# 0 = fluid (internal), 1 = wall, 2 = inlet, 3 = outlet
# "wall_for_distance": True: Wall patches are distance 0 from themselves
# Treat open as wall here
BOUNDARY_CONFIG = {
    "wall":   {"wall_for_distance": True,  "flag": 1},
    # "open":   {"wall_for_distance": True,  "flag": 1}, 
    "inlet":  {"wall_for_distance": False, "flag": 2},
    "outlet": {"wall_for_distance": False, "flag": 3},
    # "airfoil": {"wall_for_distance": True,  "flag": 1},
}

# --- Computational Resources ---
OF_SOLVER       = "simpleFoam"  # "simpleFoam" or "interFoam"
NUM_CORES       = 10
NUM_SIMULATIONS = 200
NUM_INFERENCE   = 1
MAX_ITERATIONS  = 1000
MESH_SCALE_FACTOR = 0.3

# -----------------------------DEEP LEARNING  --------------------------------------
# ----------------------------CHANGE FOR CASE --------------------------------
BATCH_SIZE = 16
NUM_POINTS = 16000
EPOCH = 2000
DL_SOLVER = "pointTransformer"  # "PointNetPP", "PointNetV2", "PointNetPP", "pointTransformer", "PointTransformerV3", "GNNFluid", or "MeshGraphNet" or "PointNet_Conv"
HIDDEN_NEURO_SCALING = 1.0
GNN_K = 3  # k-nearest neighbours for GNNFluid (EdgeConv) graph construction

# --- Optimizer (Muon+AdamW) ---
MUON_LR = 0.005    # learning rate for Muon-optimized parameters (conv/linear weights)
ADAM_LR = 0.0005   # learning rate for AdamW-optimized parameters (biases, norms, embeddings)

# --- MeshGraphNet (real mesh edge connectivity from .vtu files) ---
# Requires OpenFOAM results with foamToVTK exported internal.vtu files.
# batch_size is forced to 1 automatically (each mesh has a different edge count).
MGN_NODE_DIM = 128   # hidden node feature width
MGN_EDGE_DIM = 128   # hidden edge feature width
MGN_LAYERS   = 6     # number of message-passing rounds
# MGN_VTU_DIR: root directory containing <case>/VTK/<time>/internal.vtu files.
# Defaults to RESULTS_DIR (set below) — override here if VTU files are elsewhere.

# --- Multi-GPU ---
# 0  → CPU only
# 1  → single GPU (default, no DataParallel)
# N  → DataParallel across the first N GPUs
# -1 → DataParallel across ALL available GPUs
GPU_COUNT = 1

# Point sampling strategy used in DatasetSamplingAllWalls.__getitem__:
#   "random"      — boundary first then random fluid fill (fast, default)
#   "random_pure" — pure random from all points; no guaranteed boundary inclusion
#   "fps"         — boundary first then Farthest Point Sampling for fluid fill
#                   (better spatial coverage, ~2–5× slower per sample)
# Note: when DL_SOLVER is PointNetPP/PointNetPPKNN the dataset sampling is
# automatically frozen per sample index (deterministic=True) so the model's
# internal FPS/grouping sees a stable input across epochs.
SAMPLING_STRATEGY = "random_pure"


# --- Logging ---
# LOG_LEVEL    : verbosity — "DEBUG", "INFO", "WARNING", "ERROR"
# LOG_TO_FILE  : True  → write log to LOG_FILE_PATH in addition to console
#                False → console only
# LOG_FILE_PATH: path for the log file (created/appended automatically)
LOG_LEVEL     = "INFO"
LOG_TO_FILE   = True
LOG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "training.log")

# --- Weights & Biases ---
# WANDB_MODE: "online"   → live dashboard (requires login: wandb login)
#             "offline"  → saves locally; sync later with: wandb sync
#             "disabled" → no wandb at all, console logging only
WANDB_MODE = "disabled"


# ----------------------------NO NEED TO CHANGE --------------------------------

# Field metadata — drives channel counts and stats keys automatically
FIELD_CONFIG = {
    "U":          {"channels": 3, "stats_keys": ["u_mean", "u_std", "v_mean", "v_std", "w_mean", "w_std"]},
    "p":          {"channels": 1, "stats_keys": ["pressure_mean", "pressure_std"]},
    "p_rgh":      {"channels": 1, "stats_keys": ["p_rgh_mean", "p_rgh_std"]},
    "k":          {"channels": 1, "stats_keys": ["k_mean", "k_std"]},
    "omega":      {"channels": 1, "stats_keys": ["omega_mean", "omega_std"]},
    "alpha.water":{"channels": 1, "stats_keys": ["alpha.water_mean", "alpha.water_std"]},
    "grad(U)":    {"channels": 9, "stats_keys": [
        "gradU_xx_mean", "gradU_xx_std", "gradU_xy_mean", "gradU_xy_std", "gradU_xz_mean", "gradU_xz_std",
        "gradU_yx_mean", "gradU_yx_std", "gradU_yy_mean", "gradU_yy_std", "gradU_yz_mean", "gradU_yz_std",
        "gradU_zx_mean", "gradU_zx_std", "gradU_zy_mean", "gradU_zy_std", "gradU_zz_mean", "gradU_zz_std",
    ]},
    "grad(p)":    {"channels": 3, "stats_keys": [
        "gradp_x_mean", "gradp_x_std", "gradp_y_mean", "gradp_y_std", "gradp_z_mean", "gradp_z_std",
    ]},
}

# Derived — total channel counts for model I/O
INPUT_CHANNELS  = sum(FIELD_CONFIG[f]["channels"] for f in TRAIN_FIELDS)
OUTPUT_CHANNELS = sum(FIELD_CONFIG[f]["channels"] for f in PREDICT_FIELDS)

# Derived — boundary patch names from BOUNDARY_CONFIG flags
INLET_PATCH_NAME  = next((k for k, v in BOUNDARY_CONFIG.items() if v["flag"] == 2), "inlet")
OUTLET_PATCH_NAME = next((k for k, v in BOUNDARY_CONFIG.items() if v["flag"] == 3), "outlet")

# --- Directory Paths ---
WORK_DIR       = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR    = os.path.join(WORK_DIR, "results")
MGN_VTU_DIR     = os.path.join(WORK_DIR, "parametric_study")    # <case>/case/VTK/<time>/internal.vtu
MGN_DATASET_DIR = os.path.join(WORK_DIR, "dataset_meshgraphnet") # pre-processed .npz files
QUARANTINE_DIR = os.path.join(WORK_DIR, "quarantine")
DATASET_DIR    = os.path.join(WORK_DIR, "dataset")
INFERENCE_DIR  = os.path.join(WORK_DIR, "inference_output")
TEMPLATE_DIR   = os.path.join(WORK_DIR, "openfoam_base", GEO_NAME)



