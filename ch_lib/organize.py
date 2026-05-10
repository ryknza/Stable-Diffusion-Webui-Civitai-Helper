""" -*- coding: UTF-8 -*-
Organize models based on their metadata.
This module provides functionality to move models into subfolders based on
author (creator) and/or base model version.
"""
import os
import sys
import json
import shutil
import re
from pathlib import Path
import gradio as gr
from . import util
from . import model

MODEL_CATEGORIES = {
    "character",
    "style",
    "celebrity",
    "concept",
    "clothing",
    "base model",
    "poses",
    "background",
    "tool",
    "buildings",
    "vehicle",
    "objects",
    "animal",
    "action",
    "assets"
}

BASE_MODEL_MAPPING = {
    "SD 1.5": "SD15",
    "SD 1.4": "SD14",
    "SD 2.0": "SD20",
    "SD 2.1": "SD21",
    "SDXL 1.0": "SDXL",
    "SDXL 0.9": "SDXL",
    "Pony Diffusion V6 XL": "Pony",
    "Illustrious": "Illu",
    "NoobAI": "Noob",
    "Wan Video 2.2 I2V-A14B": "WanVideo22",
    "Anima": "Anima",
    "Flux.1 S": "Flux1",
    "Flux.1 D": "Flux1",
    "SD 3": "SD3"
}

def sanitize_filename(name):
    """Replace characters that cannot be used in file or folder names"""
    return re.sub(r'[\\/:*?"<>|]+', '_', name).strip()

def get_unique_stem(directory, stem, extension):
    """
    Returns a new filename (without extension) with _1, _2 appended if a file with the same name exists in the destination.
    Duplicate detection is based on the file extension.
    """
    # Check with the original name first
    if not (directory / f"{stem}{extension}").exists():
        return stem
    
    # Append sequence number if duplicate
    counter = 1
    while True:
        new_stem = f"{stem}_{counter}"
        if not (directory / f"{new_stem}{extension}").exists():
            return new_stem
        counter += 1

def organize(model_types, organize_by_author=True, organize_by_base_model=True, organize_by_category=False, remove_empty_folders=False, progress=gr.Progress()):
    """
    Organize function called from WebUI
    """
    if not model_types:
        msg = "No model types selected."
        util.printD(msg)
        yield f"⚠️ {msg}"
        return

    # Get exclude filters from settings
    exclude_paths = util.get_opts("ch_organize_exclude_paths")

    excludes = []
    if exclude_paths:
        excludes = [p.strip().lower().replace("\\", "/").strip("/") for p in exclude_paths.split(",") if p.strip()]

    if excludes:
        msg = f"Exclude filters: {excludes}"
        util.printD(msg)
    
    final_results = []

    for model_type in model_types:
        if model_type not in model.folders:
            continue
        
        target_p = Path(model.folders[model_type])
        if not target_p.exists():
            continue

        msg = f"--- Organizing {model_type}: {target_p} ---"
        util.printD(msg)

        # Scan all model files in the directory
        count = 0
        model_files = []
        for root, _, files in os.walk(target_p, followlinks=True):
            for filename in files:
                file_path = Path(root) / filename
                if file_path.suffix.lower() in model.EXTS:
                    model_files.append(file_path)

        total_files = len(model_files)
        scan_msg = f"Scanning {total_files} files in {model_type}..."
        util.printD(scan_msg)
        progress((0, total_files), desc=scan_msg, unit="files")

        excluded_count = 0
        no_info_count = 0
        already_organized_count = 0
        path_too_long_count = 0
        for i, file in enumerate(model_files):
            progress((i, total_files), desc=f"Organizing {model_type}...", unit="files")

            if not file.exists():
                continue

            # Check filters (Exclude folders)
            try:
                # Check relative path to avoid matching the root folder name
                rel_path = file.parent.relative_to(target_p)
            except ValueError:
                continue

            # Split path into parts (folders) and convert to lower case
            # e.g. "Lora/Characters/Miku" -> ["lora", "characters", "miku"]
            if str(rel_path) == ".":
                path_parts = []
            else:
                path_parts = [part.lower() for part in rel_path.parts]

            if excludes and any(ex in path_parts for ex in excludes):
                excluded_count += 1
                continue

            # Check for metadata file (.civitai.info)
            info_path = file.with_suffix(".civitai.info")
            if not info_path.exists():
                no_info_count += 1
                continue

            creator = "Unknown_Author"
            base_model = None
            category = None
            
            current_stem = file.stem
            desired_stem = current_stem

            # Load metadata
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Get creator name
                raw_creator = data.get("creator", {}).get("username", "Unknown_Author")
                creator = sanitize_filename(raw_creator)

                # Get base model if enabled
                if organize_by_base_model:
                    raw_base_model = data.get("baseModel")
                    if raw_base_model:
                        mapped_model = BASE_MODEL_MAPPING.get(raw_base_model, raw_base_model)
                        base_model = sanitize_filename(mapped_model)
                    else:
                        base_model = "Unknown_Base_Model"
                
                # Get category if enabled
                if organize_by_category:
                    tags = data.get("tags", [])
                    for tag in tags:
                        tag_name = tag if isinstance(tag, str) else tag.get("name", "")
                        if tag_name in MODEL_CATEGORIES:
                            category = sanitize_filename(tag_name)
                            break

                # Use filename information from .civitai.info
                files_info = data.get("files", [])
                if isinstance(files_info, list) and files_info:
                    # Prioritize primary=True, otherwise use the first in the list
                    target_info = next((f for f in files_info if f.get("primary") is True), files_info[0])
                    if "name" in target_info:
                        # Get filename without extension (stem)
                        desired_stem = Path(sanitize_filename(target_info["name"])).stem
            except Exception as e:
                util.printD(f"JSON load error: {info_path.name} -> {e}")
                continue

            # 2. Determine destination folder and skip processed files
            # Folder hierarchy examples based on selected options:
            # - All ON:                [target_dir] / [Base Model] / [Category] / [Author]
            # - Base Model + Category: [target_dir] / [Base Model] / [Category]
            # - Base Model + Author:   [target_dir] / [Base Model] / [Author]
            # - Category + Author:     [target_dir] / [Category] / [Author]
            # - Only Author:           [target_dir] / [Author]
            # - Only Category:         [target_dir] / [Category]
            # - Only Base Model:       [target_dir] / [Base Model]
            target_dir = target_p

            if organize_by_base_model and base_model:
                target_dir = target_dir / base_model

            if organize_by_category and category:
                target_dir = target_dir / category

            if organize_by_author:
                target_dir = target_dir / creator

            target_dir.mkdir(parents=True, exist_ok=True)

            # If already in the correct folder
            if file.parent == target_dir:
                # Skip if name matches exactly or matches "original_name_number"
                # (Prevent infinite renaming like _1 to _2, _3...)
                if current_stem == desired_stem or re.match(rf"^{re.escape(desired_stem)}_\d+$", current_stem):
                    already_organized_count += 1
                    continue

            # 3. Determine name to avoid duplicates (Rename per set)
            new_stem = get_unique_stem(target_dir, desired_stem, file.suffix)

            if current_stem != new_stem:
                rename_msg = f"Rename: {current_stem} -> {new_stem}"
                util.printD(rename_msg)

            # 4. Move related files together
            target_extensions = list(model.EXTS) + [".png", ".jpg", ".jpeg", ".preview.png", ".civitai.info", ".json", ".txt"]

            # Pre-check path length for Windows (MAX_PATH = 260)
            if os.name == 'nt':
                path_too_long = False
                for ext in target_extensions:
                    if (file.parent / (current_stem + ext)).exists():
                        if len(str((target_dir / (new_stem + ext)).absolute())) >= 260:
                            path_too_long = True
                            break
                if path_too_long:
                    util.printD(f"Move skipped (Path too long): {target_dir / new_stem}")
                    path_too_long_count += 1
                    continue

            for ext in target_extensions:
                src_file = file.parent / (current_stem + ext)
                
                if src_file.exists():
                    dest_file = target_dir / (new_stem + ext)
                    try:
                        shutil.move(str(src_file), str(dest_file))
                    except Exception as e:
                        util.printD(f"Move failed: {src_file.name} -> {e}")
            
            count += 1
            if count % 50 == 0:
                prog_msg = f"Organizing: {count} sets done..."
                util.printD(prog_msg)

        progress((total_files, total_files), desc=f"Finished organizing {model_type}", unit="files")

        # Delete empty folders
        if remove_empty_folders:
            clean_msg = "Cleaning up empty folders..."
            util.printD(clean_msg)
            progress((total_files, total_files), desc=clean_msg, unit="files")
            for dirpath, _, _ in os.walk(str(target_p), topdown=False):
                if os.path.abspath(dirpath) == os.path.abspath(str(target_p)): continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except: pass

        result_msg = f"{model_type}: {count} organized. (Excluded: {excluded_count}, No Info: {no_info_count}, Already Organized: {already_organized_count}"
        if path_too_long_count > 0:
            result_msg += f", Path too long: {path_too_long_count}"
        result_msg += ")"
        util.printD(result_msg)
        final_results.append(f"✨ {result_msg}")

    yield "<br>".join(final_results)
