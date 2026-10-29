"""Static build only: no production secrets or provider calls."""
from .settings import *
DEBUG=False
STATIC_ROOT=BASE_DIR/'staticfiles'
STATIC_URL='/static/'
STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
