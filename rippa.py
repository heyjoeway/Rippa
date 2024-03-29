#!/usr/bin/env python3

import subprocess
from typing import Optional
import logging
import re
import time
import pathlib
import shutil
import os
import atexit
import threading

from makemkvkey import updateMakeMkvKey

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

def rip_dvd(
        blkid_params: dict,
        drive: str,
        wip_root: str,
        out_root: str,
        makemkv_update_key: bool,
        makemkv_settings_path: Optional[str] = None
    ):
    disc_name = f"{blkid_params['LABEL']}-{blkid_params['UUID']}"
    wip_path = f"{wip_root}/dvd/{disc_name}"
    out_path = f"{out_root}/{disc_name}"
    
    # Check if output path exists
    if pathlib.Path(out_path).exists():
        logging.info(f"Output path exists: {out_path}")
        return
    
    if makemkv_update_key:
        updateMakeMkvKey(makemkv_settings_path)
    
    logging.info(f"Ripping DVD: {disc_name}")
    
    shutil.rmtree(wip_path, ignore_errors=True)
    os.makedirs(wip_path, exist_ok=True)
    os.makedirs(out_path, exist_ok=True)
    
    # Get number of the drive (eg. 0 for /dev/sr0, 1 for /dev/sr1, etc.)
    drive_id = int(re.search(r'\d+', drive).group(0))
    o_path_abs = os.path.abspath(wip_path)
    
    cmd = [
        "makemkvcon", "mkv",
        f"disc:{drive_id}",
        "all", f"{o_path_abs}"
    ]
    logging.debug(f"Executing: {' '.join(cmd)}")
    execute(cmd, capture=False)
    
    # Transcoding is handled in its own thread, so we're done here!

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
        "-B", "-N"
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
    
_mounts = []
def mount(drive: str, mnt_path: str):
    os.makedirs(mnt_path, exist_ok=True)
    execute(["sudo", "mount", drive, mnt_path])
    _mounts.append(mnt_path)

def unmount(mnt_path: str):
    execute(["sudo", "umount", mnt_path])

@atexit.register
def mount_cleanup():
    for mnt in _mounts:
        unmount(mnt)

def transcode_file(file_path: str, out_path: str):
    # Check if file size is changing
    size1 = os.path.getsize(file_path)
    logging.debug(f"size1: {size1}")
    
    # Skip if less than 1MB
    if size1 < 1024 * 1024:
        logging.debug(f"File {file_path} is less than 1MB")
        return
    
    time.sleep(30)
    size2 = os.path.getsize(file_path)
    logging.debug(f"size2: {size2}")
    if size1 != size2:
        logging.debug(f"File {file_path} is not done being written (file size changed)")
        return       
    
    # Check if file is done being written
    # TODO: This doesn't work? 
    # try:
    #     with open(file_path, "a") as f:
    #         logging.debug(f"File {file_path} is not currently being written to")
    # except FileNotFoundError:
    #     logging.debug(f"File {file_path} does not exist")
    #     return
    # except Exception as e:
    #     logging.debug(f"File {file_path} is currently being written to: {e}")
    #     return
    
    os.makedirs(out_path, exist_ok=True)
    
    file_name = os.path.basename(file_path)
    file_no_ext = os.path.splitext(file_name)[0]
    out_file_path = f"{out_path}/{file_no_ext}.mp4"
    cmd = [
        "ffmpeg",
        "-i", file_path,
        "-c:v", "libx264",
        "-crf", "18",
        "-map", "0",
        "-c:a", "copy",
        "-c:s", "copy",
        out_file_path
    ]
    logging.debug(f"Executing: {' '.join(cmd)}")
    execute(cmd, capture=False)
    
    logging.debug(f"Removing file: {file_path}")
    os.remove(file_path)

def transcode_disc(disc_name: str, wip_dvd_root: str, out_root: str):
    wip_path = f"{wip_dvd_root}/{disc_name}"
    out_path = f"{out_root}/{disc_name}"
    
    files = os.listdir(wip_path)
    logging.debug(f"Files: {files}")

    for file in files:
        file_path = f"{wip_path}/{file}"
        try:
            transcode_file(file_path, out_path)
        except Exception as e:
            logging.debug(f"transcode_file error: {e}")
            logging.debug(f"Maybe MakeMKV is still ripping the disc?")

def transcode_loop_step(wip_root: str, out_root: str):
    logging.debug("Transcode loop step")
    wip_dvd_root = f"{wip_root}/dvd"
    for disc_name in os.listdir(wip_dvd_root):
        logging.debug(f"Transcoding disc: {disc_name}")
        transcode_disc(disc_name, wip_dvd_root, out_root)

class StoppableThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

class TranscodeThread(StoppableThread):
    def __init__(self, wip_root, out_root):
        super().__init__()
        self.wip_root = wip_root
        self.out_root = out_root

    def run(self):
        while not self.stopped():
            transcode_loop_step(self.wip_root, self.out_root)
            time.sleep(5)

def main_loop_step(
    drive: str,
    wip_path: str,
    dvd_path: str,
    redbook_path: str,
    iso_path: str,
    bluray_path: str,
    skip_eject: bool,
    makemkv_update_key: bool,
    makemkv_settings_path: Optional[str] = None
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
            if not skip_eject:
                eject(drive)
        except subprocess.CalledProcessError as e:
            logging.debug("No disc detected")
        return
    
    logging.debug(f"blkid_str: {blkid_str}")
    blkid_params = parse_blkid(blkid_str)[drive]
    logging.debug(f"params: {blkid_params}")
    
    mnt_path = f"./mnt{drive}"
    try:
        mount(drive, mnt_path)
    except Exception as e:
        logging.debug("mount error: %s", e)
    
    # Check if "VIDEO_TS" exists
    if pathlib.Path(f"{mnt_path}/VIDEO_TS").exists():
        rip_dvd(
            blkid_params,
            drive,
            wip_path,
            dvd_path,
            makemkv_update_key,
            makemkv_settings_path
        )
    else:
        rip_data_disc(blkid_params, drive, wip_path, iso_path)
    
    if not skip_eject:
        eject(drive)

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--drive", default="/dev/sr0", help="Path to the optical drive")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--wip-path", default="./wip", help="Path to store work-in-progress files")
    parser.add_argument("--dvd-path", default="./dvd", help="Path to rip dvd discs to")
    parser.add_argument("--redbook-path", default="./redbook", help="Path to rip redbook audio discs to")
    parser.add_argument("--iso-path", default="./iso", help="Path to rip data discs to")
    parser.add_argument("--bluray-path", default="./bluray", help="Path to rip bluray discs to")
    parser.add_argument("--skip-eject", action="store_true", help="Don't eject the disc after ripping")
    parser.add_argument("--makemkv-update-key", action="store_true", help="Automatically update free MakeMKV key")
    parser.add_argument(
        "--makemkv-settings-path",
        help="Path to the MakeMKV settings file",
        default="~/.MakeMKV/settings.conf"
    )
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    
    transcode_thread = TranscodeThread(args.wip_path, args.dvd_path)
    transcode_thread.start()
    
    while True:
        try:
            main_loop_step(
                args.drive,
                args.wip_path,
                args.dvd_path,
                args.redbook_path,
                args.iso_path,
                args.bluray_path,
                args.skip_eject,
                args.makemkv_update_key,
                args.makemkv_settings_path
            )
        except Exception as e:
            logging.debug("main_loop_step error: %s", e)
        time.sleep(2)
    
    transcode_thread.stop()
    transcode_thread.join()