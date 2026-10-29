"""Small single-instance deployment; local config.settings stays unchanged."""
from .settings import *
import os
import re
from django.core.exceptions import ImproperlyConfigured

def required(name):
    value=os.environ.get(name,'').strip()
    if not value or value.startswith('CHANGE_'):raise ImproperlyConfigured('缺少服务器配置：'+name)
    return value

SECRET_KEY=required('DJANGO_SECRET_KEY')
if len(SECRET_KEY)<50 or SECRET_KEY.startswith('django-insecure-'):raise ImproperlyConfigured('请生成新的服务器SECRET_KEY。')
SITE_DOMAIN=required('RENDER_EXTERNAL_HOSTNAME').lower()
if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}',SITE_DOMAIN):raise ImproperlyConfigured('SITE_DOMAIN需要填写域名，不带https或路径。')
if not re.fullmatch('[a-fA-F0-9]{32}',required('TRAVEL_AMAP_KEY')):raise ImproperlyConfigured('高德Web服务Key格式不正确。')
if len(required('TUNIU_API_KEY'))<8:raise ImproperlyConfigured('途牛API Key格式不正确。')
DEBUG=False
ALLOWED_HOSTS=[SITE_DOMAIN,'localhost','127.0.0.1']
CSRF_TRUSTED_ORIGINS=['https://'+SITE_DOMAIN]
SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')
SECURE_SSL_REDIRECT=True
SECURE_REDIRECT_EXEMPT=[r'^healthz/$']
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE='Lax'
SECURE_CONTENT_TYPE_NOSNIFF=True
SECURE_HSTS_SECONDS=3600
SECURE_HSTS_INCLUDE_SUBDOMAINS=False
SECURE_HSTS_PRELOAD=False
X_FRAME_OPTIONS='DENY'
LANGUAGE_CODE='zh-hans'
TIME_ZONE='Asia/Shanghai'
ROOT_URLCONF='config.render_urls'
STATIC_URL='/static/'
STATIC_ROOT=BASE_DIR/'staticfiles'
STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
MIDDLEWARE.insert(1,'whitenoise.middleware.WhiteNoiseMiddleware')
MIDDLEWARE.append('travel.deployment_gate.DeploymentGate')
from .render_database import parse_database_url
DATABASES={'default':parse_database_url(required('DATABASE_URL'))}
CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache','LOCATION':'single-instance-travel-online','OPTIONS':{'MAX_ENTRIES':2000}}}
DATA_UPLOAD_MAX_MEMORY_SIZE=1_000_000
FILE_UPLOAD_MAX_MEMORY_SIZE=1_000_000
TRAVEL_SESSION_STARTS_PER_HOUR=3
TRAVEL_GLOBAL_STARTS_PER_HOUR=15
# Current provider limiter and deployment gate are process-local. Keep ONE worker/instance.
LOGGING={'version':1,'disable_existing_loggers':False,'handlers':{'console':{'class':'logging.StreamHandler'}},'root':{'handlers':['console'],'level':'WARNING'},'loggers':{'django.request':{'handlers':['console'],'level':'ERROR','propagate':False}}}

# This site does not control every subdomain and is not enrolling in HSTS preload.
SILENCED_SYSTEM_CHECKS=['security.W005','security.W021']
