#!/bin/bash
# Example CLI usage for tiny-vlm-360

# Basic usage
python -m tiny_vlm_360.cli path/to/pano.jpg

# With custom configs
python -m tiny_vlm_360.cli path/to/pano.jpg \
    --config configs/default.yaml \
    --model-config configs/models.minicpm.yaml

# Save output to file
python -m tiny_vlm_360.cli path/to/pano.jpg \
    --output results.json

# Verbose mode
python -m tiny_vlm_360.cli path/to/pano.jpg --verbose

# Using Qwen2-VL model
python -m tiny_vlm_360.cli path/to/pano.jpg \
    --model-config configs/models.qwen2vl.yaml

