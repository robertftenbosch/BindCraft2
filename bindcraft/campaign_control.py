"""Pausing a campaign and picking it up again.

A trajectory is the unit of work worth keeping: it costs minutes of GPU time and it is only written to
the tables once it finishes. So a pause is asked for rather than taken. The campaign folder carries a
sentinel file, every worker reads it between trajectories, and the one it is on is allowed to finish
and be recorded. That makes `bindcraft resume` a matter of claiming the next trajectory, which the
campaign state already knows how to do.
"""
import json
import os
import signal
import time
from pathlib import Path
from bindcraft.campaign_log import campaign_pause_asked, campaign_resumed, design_worker_index
from bindcraft.campaign_output import CAMPAIGN_METADATA_NAME, json_compatible

PAUSE_FILENAME = '.campaign_pause'
RESUME_SETTINGS_FILENAME = '.resume_settings.json'

def campaign_pause_path(project_folder: str) -> str:
    return os.path.join(project_folder, PAUSE_FILENAME)

def request_campaign_pause(project_folder: str) -> str:
    os.makedirs(project_folder, exist_ok=True)
    pause_path = campaign_pause_path(project_folder)
    Path(pause_path).write_text(time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
    return pause_path

def campaign_pause_requested(project_folder: str) -> bool:
    return os.path.exists(campaign_pause_path(project_folder))

def clear_campaign_pause(project_folder: str) -> bool:
    try:
        os.remove(campaign_pause_path(project_folder))
    except OSError:
        return False
    return True

def pause_on_interrupt(project_folder: str) -> None:
    """Ctrl+C asks for the pause instead of killing the run; a second one drops the trajectory in flight.

    Both the campaign and every design worker install this, since an interrupt in the terminal reaches
    the whole process group and asking twice for the same pause costs nothing."""
    def ask_for_the_pause(received: int, _frame) -> None:
        signal.signal(received, signal.default_int_handler if received == signal.SIGINT else signal.SIG_DFL)
        request_campaign_pause(project_folder)
        if design_worker_index() is None:
            print('\n' + campaign_pause_asked(project_folder), flush=True)
    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(received, ask_for_the_pause)
        except ValueError:  #a notebook kernel or any thread that is not the main one
            return

def recorded_campaign(project_folder: str) -> dict:
    metadata_path = os.path.join(project_folder, f'{CAMPAIGN_METADATA_NAME}.json')
    if not os.path.exists(metadata_path):
        raise ValueError(f'{project_folder} holds no {CAMPAIGN_METADATA_NAME}.json, so no campaign was started there to carry on')
    return json.loads(Path(metadata_path).read_text())

def resume_settings_path(project_folder: str, record: dict) -> str:
    """The file the campaign started from, or the settings recorded beside its output once that file has moved."""
    settings_path = str(record.get('settings_path') or '')
    if settings_path and os.path.exists(settings_path):
        return settings_path
    written_path = os.path.join(project_folder, RESUME_SETTINGS_FILENAME)
    Path(written_path).write_text(json.dumps(json_compatible(record.get('settings') or {}), indent=2, sort_keys=True))
    return written_path

def resume_overrides(project_folder: str, record: dict) -> list[str]:
    """Only what the recorded settings do not already say, so carrying on records the settings it started from rather than a second campaign.

    A campaign resumes by default, and the folder is named in the settings unless it was moved since."""
    settings = record.get('settings') or {}
    recorded_folder = str(settings.get('project_folder') or '')
    overrides = [] if recorded_folder and os.path.abspath(recorded_folder) == os.path.abspath(project_folder) else [f'project_folder={project_folder}']
    return overrides + ([] if settings.get('resume', True) else ['resume=true'])

def pause_campaign(project_folder: str) -> int:
    if not os.path.isdir(project_folder):
        raise ValueError(f'{project_folder} is no campaign folder')
    request_campaign_pause(project_folder)
    print(campaign_pause_asked(project_folder), flush=True)
    return 0

def resume_campaign(project_folder: str) -> int:
    from bindcraft.campaign import launch_campaign
    record = recorded_campaign(project_folder)
    settings_path = resume_settings_path(project_folder, record)
    clear_campaign_pause(project_folder)
    print(campaign_resumed(project_folder, settings_path), flush=True)
    return launch_campaign(settings_path, resume_overrides(project_folder, record))
