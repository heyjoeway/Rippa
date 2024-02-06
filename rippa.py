#!/usr/bin/env python3

import subprocess
from typing import Optional
import logging
import re
import time
import pathlib
import shutil
import os

def log_subprocess_output(pipe):
    for line in iter(pipe.readline, b''): # b'\n'-separated lines
        logging.info('SUBPROCESS: %r', line.decode("utf-8"))

# Timeout is in seconds
def execute(cmd, capture=True, cwd=None) -> Optional[str]:
    if capture:
        return subprocess.check_output(
            cmd, cwd=cwd, stderr=subprocess.STDOUT
        ).strip().decode("utf-8")
    
    process = subprocess.Popen(
        cmd, cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    with process.stdout:
        log_subprocess_output(process.stdout)
    exitcode = process.wait() # 0 means success
    if (exitcode != 0):
        raise Exception(exitcode)
    
    return None

# Params are in format:
# A="B" C="D" E="F"
# Values CAN contain spaces, but they are always quoted
def parse_blkid_params(params_str: str) -> dict:
    params = {}
    for match in re.finditer(r'(\w+)="([^"]+)"', params_str):
        key = match.group(1)
        value = match.group(2)
        params[key] = value
    return params

def parse_blkid(blkid_str: str) -> dict:
    blkid = {}
    for line in blkid_str.split("\n"):
        if not line:
            continue
        parts = line.split(": ")
        blk = parts[0]
        params_str = parts[1]
        params = parse_blkid_params(params_str)
        blkid[blk] = params
    return blkid

def rip_dvd(blkid_params: dict, drive: str, wip_root: str, out_root: str):
    file_name = f"{blkid_params['LABEL']}-{blkid_params['UUID']}.mp4"
    wip_dir_path = f"{wip_root}/dvd"
    out_dir_path = out_root
    wip_path = f"{wip_dir_path}/{file_name}"
    out_path = f"{out_dir_path}/{file_name}"
    
    # Check if output path exists
    if pathlib.Path(out_path).exists():
        logging.info(f"Output path exists: {out_path}")
        return
    
    logging.info(f"Ripping DVD: {file_name}")
    
    os.makedirs(wip_dir_path, exist_ok=True)
    os.makedirs(out_dir_path, exist_ok=True)
    
    i_path_abs = drive
    o_path_abs = os.path.abspath(wip_path)
    
    # Rip VIDEO_TS/VTS_0*_*.VOB to {wip_folder_path}/out.mp4
    cmd = [
        "HandBrakeCLI",
        "-i", f"{i_path_abs}",
        "-o", f"{o_path_abs}",
        "-e", "x264",
        "-q", "20", # Video quality
        "-B", "256" # Audio bitrate
    ]
    logging.debug(f"Executing: {' '.join(cmd)}")
    execute(cmd, capture=False)
    
    # Move the file to the out folder
    shutil.move(wip_path, out_path)

def cdparanoia_hash(cdp_str: str) -> int:
    lines = cdp_str.split('\n')[6:-2]
    lengths = []
    for line in lines:
        split = line.split()
        if len(split) != 8:
            continue
        
        lengths.append(int(split[1]))
    return hash(tuple(lengths))

def rip_redbook(cdp_str: str, drive: str, out_root: str, wip_root: str):
    cdp_hash = cdparanoia_hash(cdp_str)
    cdp_hash = str(hex(abs(cdp_hash)))[2:]
    
    # Check if any folders in out begin with the hash
    out_dir_path = out_root
    os.makedirs(out_dir_path, exist_ok=True)
    for folder in os.listdir(out_dir_path):
        if folder.endswith(cdp_hash):
            logging.info(f"Redbook already ripped: {folder}")
            return
    
    logging.info(f"Ripping redbook: {cdp_hash}")
    
    wip_dir_path = f"{wip_root}/redbook"
    os.makedirs(wip_dir_path, exist_ok=True)
    
    pwd = os.getcwd()
    os.chdir(wip_dir_path)
    cmd = [
        "abcde",
        "-d", drive,
        "-o", "flac",
        "-B", "-x", "-N"
    ]
    execute(cmd, capture=False)
    os.chdir(pwd)
    
    # Get name of first directory in wip folder
    album_name = os.listdir(wip_dir_path)[0]
    
    out_path = f"{out_dir_path}/{album_name}-{cdp_hash}"
    shutil.move(f"{wip_dir_path}/{album_name}", out_path)

def rip_data_disc(blkid_params: dict, drive: str, wip_root: str, out_root: str):
    file_name = f"{blkid_params['LABEL']}-{blkid_params['UUID']}.iso"
    wip_dir_path = f"{wip_root}/iso"
    out_dir_path = out_root
    wip_path = f"{wip_dir_path}/{file_name}"
    out_path = f"{out_dir_path}/{file_name}"
    
    # Check if output path exists
    if pathlib.Path(out_path).exists():
        logging.info(f"Output path exists: {out_path}")
        return
    
    logging.info(f"Ripping data disc: {file_name}")
    
    os.makedirs(wip_dir_path, exist_ok=True)
    os.makedirs(out_dir_path, exist_ok=True)
    
    cmd = [
        "dd" 
        f"if={drive}",
        f"of={out_path}",
        "status=progress"
    ]
    logging.debug(f"Executing: {' '.join(cmd)}")
    execute(cmd, capture=False, cwd=os.getcwd())
    
    # Move the file to the out folder
    shutil.move(wip_path, out_path)

def rip_bluray(blkid_params: dict):
    pass

def eject(drive: str):
    execute(["sudo", "eject", "-F", drive])
    
def mount(drive: str, mnt_path: str):
    os.makedirs(mnt_path, exist_ok=True)
    execute(["sudo", "mount", drive, mnt_path])

def main_loop_step(
    drive: str,
    wip_path: str,
    dvd_path: str,
    redbook_path: str,
    iso_path: str,
    bluray_path: str
):
    blkid_str = None
    try:
        blkid_str = execute(["blkid", drive], capture=True)
    except Exception as e:
        logging.debug("blkid error: %s", e)
    
    if (blkid_str is None) or (len(blkid_str) == 0):
        logging.debug("No blkid output")
        # This COULD mean that it's a redbook disc.
        # To check, we'll run cdparanoia -sQ and get the return code.
        # If it's 0, it's a redbook disc.
        try:
            cdp_text = execute(["cdparanoia", "-sQ"], capture=True)
            logging.info("Redbook disc detected")
            rip_redbook(cdp_text, drive, redbook_path, wip_path)
            eject(drive)
        except subprocess.CalledProcessError as e:
            logging.debug("No disc detected")
        return
    
    logging.debug(f"blkid_str: {blkid_str}")
    params = parse_blkid(blkid_str)[drive]
    logging.debug(f"params: {params}")
    
    mnt_path = f"./mnt{drive}"
    try:
        mount(drive, mnt_path)
    except Exception as e:
        logging.debug("mount error: %s", e)
    
    # Check if "VIDEO_TS" exists
    if pathlib.Path(f"{mnt_path}/VIDEO_TS").exists():
        rip_dvd(params, drive, wip_path, dvd_path)
    else:
        rip_data_disc(params, drive, wip_path, iso_path)
    
    eject(drive)

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive", default="/dev/sr0")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--wip-path", default="./wip")
    parser.add_argument("--dvd-path", default="./dvd")
    parser.add_argument("--redbook-path", default="./redbook")
    parser.add_argument("--iso-path", default="./iso")
    parser.add_argument("--bluray-path", default="./bluray")
    args = parser.parse_args()
    
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    
    while True:
        try:
            main_loop_step(
                args.drive,
                args.wip_path,
                args.dvd_path,
                args.redbook_path,
                args.iso_path,
                args.bluray_path
            )
        except Exception as e:
            logging.debug("main_loop_step error: %s", e)
        time.sleep(2)
