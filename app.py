from pathlib import Path
from urllib.parse import urlparse
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from threading import BoundedSemaphore, Lock
from time import monotonic
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import hmac
import logging
import os
import shutil
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'
MAX_FILE_SIZE = 150 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 180
PASSWORD = os.environ.get('PINDOWN_PASSWORD', '')
SESSION_COOKIE = 'pindown_session'
SESSION_MAX_AGE = 30 * 24 * 60 * 60
logger = logging.getLogger('pindown')
download_slots = BoundedSemaphore(2)
rate_lock = Lock()
rate_windows = defaultdict(deque)

class Link(BaseModel):
    url: str = Field(min_length=1, max_length=2048)

class PasswordEntry(BaseModel):
    password: str = Field(min_length=1, max_length=256)

PLATFORMS = {
    'pinterest.com': ('Pinterest', True),
    'pin.it': ('Pinterest', False),
    'www.pin.it': ('Pinterest', False),
    'instagram.com': ('Instagram', True),
    'youtube.com': ('YouTube', True),
    'youtu.be': ('YouTube', False),
    'facebook.com': ('Facebook', True),
    'fb.watch': ('Facebook', False),
}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    environment = os.environ.get('APP_ENV', 'production').lower()
    if environment not in {'development', 'test'} and len(PASSWORD) < 12:
        raise RuntimeError('Configure PINDOWN_PASSWORD com pelo menos 12 caracteres antes de expor o app.')
    if PASSWORD and len(PASSWORD) < 12:
        raise RuntimeError('PINDOWN_PASSWORD precisa ter pelo menos 12 caracteres.')
    logger.info('%s starting in %s mode', _app.title, environment)
    yield

app = FastAPI(
    title='PinDown',
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount('/static', StaticFiles(directory=STATIC), name='static')

def validate(url: str) -> tuple[str, str]:
    url = url.strip()
    parsed = None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        has_port = parsed.port is not None
    except ValueError:
        host = ''
        has_port = True

    platform = next(
        (
            name
            for domain, (name, allow_subdomains) in PLATFORMS.items()
            if host == domain or (allow_subdomains and host.endswith(f'.{domain}'))
        ),
        None,
    )
    if (
        parsed is None
        or parsed.scheme.lower() != 'https'
        or not platform
        or parsed.username is not None
        or parsed.password is not None
        or has_port
    ):
        raise HTTPException(
            400,
            'Use um link HTTPS público do Pinterest, Instagram, YouTube ou Facebook.',
        )
    return url, platform

def check_rate_limit(request: Request, endpoint: str, limit: int) -> None:
    client = request.client.host if request.client else 'unknown'
    key = (client, endpoint)
    now = monotonic()
    with rate_lock:
        for old_key, timestamps in list(rate_windows.items()):
            while timestamps and now - timestamps[0] >= 60:
                timestamps.popleft()
            if not timestamps:
                del rate_windows[old_key]
        if len(rate_windows) >= 10_000 and key not in rate_windows:
            raise HTTPException(503, 'Servidor ocupado. Tente novamente em instantes.')
        timestamps = rate_windows[key]
        if len(timestamps) >= limit:
            raise HTTPException(429, 'Muitas solicitações. Aguarde um minuto e tente novamente.')
        timestamps.append(now)

def create_session_token() -> str:
    expires_at = str(int(time.time()) + SESSION_MAX_AGE)
    signature = hmac.new(
        PASSWORD.encode('utf-8'),
        f'pindown-session:{expires_at}'.encode('ascii'),
        'sha256',
    ).hexdigest()
    return f'{expires_at}.{signature}'

def has_valid_session(request: Request) -> bool:
    if not PASSWORD:
        return True
    token = request.cookies.get(SESSION_COOKIE, '')
    try:
        expires_at, signature = token.split('.', 1)
        if int(expires_at) <= int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        PASSWORD.encode('utf-8'),
        f'pindown-session:{expires_at}'.encode('ascii'),
        'sha256',
    ).hexdigest()
    return hmac.compare_digest(signature, expected)

def require_password(request: Request) -> None:
    if not has_valid_session(request):
        raise HTTPException(401, 'Sua sessão expirou. Entre novamente.')

@app.get('/')
def index():
    return FileResponse(ROOT / 'index.html')

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.get('/api/auth/status')
def auth_status(request: Request):
    return {'authenticated': has_valid_session(request)}

@app.post('/api/auth/login')
def login(
    entry: PasswordEntry,
    request: Request,
    response: Response,
):
    check_rate_limit(request, 'login', 5)
    if not PASSWORD or not hmac.compare_digest(
        entry.password.encode('utf-8'),
        PASSWORD.encode('utf-8'),
    ):
        raise HTTPException(401, 'Senha incorreta.')
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=os.environ.get('APP_ENV', 'production').lower() not in {'development', 'test'},
        samesite='strict',
        path='/',
    )
    return {'authenticated': True}

@app.post('/api/auth/logout')
def logout(response: Response):
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=os.environ.get('APP_ENV', 'production').lower() not in {'development', 'test'},
        samesite='strict',
        path='/',
    )
    return {'authenticated': not PASSWORD}

@app.middleware('http')
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.post('/api/info', dependencies=[Depends(require_password)])
def info(
    link: Link,
    request: Request,
):
    check_rate_limit(request, 'info', 20)
    url, platform = validate(link.url)
    try:
        with YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 20,
            'retries': 1,
        }) as ydl:
            data = ydl.extract_info(url, download=False)
        if not data:
            raise ValueError('Nenhum vídeo encontrado')
        return {
            'title': data.get('title') or f'Vídeo do {platform}',
            'thumbnail': data.get('thumbnail'),
            'duration': data.get('duration'),
            'platform': platform,
        }
    except Exception as exc:
        logger.warning('Preview extraction failed for %s (%s)', platform, type(exc).__name__)
        raise HTTPException(
            422,
            f'Não foi possível obter a prévia. Verifique se o conteúdo do {platform} é público e contém vídeo.',
        ) from exc

@app.post('/api/download', dependencies=[Depends(require_password)])
def download(
    link: Link,
    request: Request,
):
    check_rate_limit(request, 'download', 5)
    url, platform = validate(link.url)
    if not download_slots.acquire(blocking=False):
        raise HTTPException(429, 'Já existem downloads em andamento. Tente novamente em instantes.')

    folder = None
    handed_off = False
    try:
        folder = Path(tempfile.mkdtemp(prefix='pindown_'))
        started_at = monotonic()
        downloaded_sizes = {}

        def enforce_download_limits(status):
            if monotonic() - started_at > MAX_DOWNLOAD_SECONDS:
                raise DownloadError('Download excedeu o tempo máximo permitido.')
            filename = status.get('filename')
            if filename and status.get('status') in {'downloading', 'finished'}:
                downloaded_sizes[filename] = status.get('downloaded_bytes') or 0
                if sum(downloaded_sizes.values()) > MAX_FILE_SIZE:
                    raise DownloadError('Download excedeu o limite de tamanho permitido.')

        options = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            'outtmpl': str(folder / '%(id)s.%(ext)s'),
            'noplaylist': True,
            'max_filesize': MAX_FILE_SIZE,
            'socket_timeout': 20,
            'retries': 2,
            'fragment_retries': 2,
            'progress_hooks': [enforce_download_limits],
        }

        with YoutubeDL(options) as ydl:
            ydl.download([url])

        # Localiza apenas o arquivo final
        files = [
            f for f in folder.iterdir()
            if f.is_file()
            and not f.name.endswith(('.part', '.ytdl'))
        ]

        if not files:
            raise ValueError('Arquivo não encontrado')

        source = max(files, key=lambda f: f.stat().st_size)

        if source.stat().st_size > MAX_FILE_SIZE:
            raise ValueError(f'O arquivo excede o limite de {MAX_FILE_SIZE // (1024 * 1024)} MB.')

        from starlette.background import BackgroundTask

        response = FileResponse(
            source,
            filename=f'pindown-{uuid.uuid4().hex[:8]}{source.suffix}',
            media_type='application/octet-stream',
            background=BackgroundTask(cleanup_download, folder),
        )
        handed_off = True
        return response

    except Exception as exc:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        logger.warning('Download failed for %s (%s)', platform, type(exc).__name__)
        raise HTTPException(422, f'Falha ao baixar. Confira se o conteúdo do {platform} é público e tente novamente.') from exc
    finally:
        if not handed_off:
            download_slots.release()

@app.get('/manifest.webmanifest')
def manifest():
    return FileResponse(ROOT / 'manifest.webmanifest', media_type='application/manifest+json')

@app.get('/sw.js')
def service_worker():
    return FileResponse(ROOT / 'sw.js', media_type='application/javascript')

def cleanup_download(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except OSError:
        logger.exception('Could not remove temporary download directory')
    finally:
        download_slots.release()