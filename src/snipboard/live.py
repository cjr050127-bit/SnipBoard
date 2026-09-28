'''Experimental session-bound log state machine; release qualification is separate.'''
from datetime import datetime
from pathlib import Path
import re

from .source import GROUP_LOG, stable_read

LINE = re.compile(r'^\[([^\]]+)\] \[I\] (.*)$')
REQUEST = re.compile(r'Request to switch to group (\d+)$')


def parse_live(log: str, process_started: datetime, history: Path) -> dict:
    current = pending = None
    restoring = False
    source_matches = False
    requested = None
    reason = '当前进程没有完整的图组恢复记录'
    for line in log.splitlines(keepends=True):
        if not line.endswith(('\r', '\n')):
            break  # A partial tail must never commit a transition.
        text = line.rstrip('\r\n')
        match = LINE.fullmatch(text)
        if not match:
            continue
        try:
            timestamp = datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S.%f')
        except ValueError:
            continue
        if timestamp < process_started:
            continue
        message = match[2]
        if message == 'Initializing Snipaste...':
            current = pending = requested = None
            restoring = False
            source_matches = False
        elif message.startswith('History dir: '):
            source_matches = Path(message[len('History dir: '):]).resolve() == history.resolve()
            if not source_matches:
                current = pending = None
        elif message.startswith('Request to switch to group '):
            request = REQUEST.fullmatch(message)
            requested = int(request[1]) if request else None
            if not request or current is None or current['index'] != requested:
                current = pending = None
                reason = '图组正在切换，尚未完成恢复'
        elif message == 'About to restore pasters...':
            current = pending = None
            restoring = True
        elif message.startswith('Group '):
            group = GROUP_LOG.fullmatch(text)
            pending = None
            if restoring and group and (requested is None or int(group['index']) == requested):
                pending = {'id': group['id'], 'name': group['name'], 'index': int(group['index'])}
        elif message == 'Pasters restored':
            if restoring and pending and source_matches:
                current = {**pending, 'observed_at': timestamp.isoformat()}
                reason = ''
            restoring = False
            pending = requested = None
    if current and source_matches:
        return {'status': 'observed', **current, 'release_qualified': False,
                'evidence': 'completed_restore_in_running_process'}
    return {'status': 'unavailable', 'id': None, 'reason': reason, 'release_qualified': False}


def detect_current(source: Path) -> dict:
    from .windows import snipaste_session
    try:
        session = snipaste_session()
        if not session:
            raise ValueError('需要恰好一个正在运行的 Snipaste 进程')
        result = parse_live(stable_read(source / 'splog.txt').decode('utf-8-sig', errors='replace'),
                            session[1], source / 'history')
        if snipaste_session() != session:
            raise ValueError('Snipaste 进程在识别期间发生变化，请重试')
        if result['id'] and not (source / 'history' / result['id']).is_dir():
            raise ValueError('识别到的图组目录暂不可用')
        return {**result, 'pid': session[0], 'process_started': session[1].isoformat()}
    except (OSError, ValueError) as exc:
        return {'status': 'unavailable', 'id': None, 'reason': str(exc), 'release_qualified': False}
