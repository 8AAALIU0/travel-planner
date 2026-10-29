"""Render entry point. No GUI prompts or root-only secrets files."""
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
os.environ['DJANGO_SETTINGS_MODULE']='config.render_settings'
import django
django.setup()
from django.core.management import call_command
from django.db import connections,transaction
from travel.models import Attraction
try:
    call_command('check',deploy=True,fail_level='WARNING')
    call_command('migrate',interactive=False,verbosity=1)
    # Import only the curated attractions and matched POIs. Never accounts/jobs/API caches.
    if not Attraction.objects.exists():
        with transaction.atomic():
            call_command('loaddata','initial_attractions.json',verbosity=0)
        print('已导入初始景点资料。',flush=True)
    # Optional administrator setup: set all three values once in Render; remove password after success.
    username=os.environ.get('BOOTSTRAP_ADMIN_USERNAME','').strip()
    password=os.environ.get('BOOTSTRAP_ADMIN_PASSWORD','')
    if username and password:
        from django.contrib.auth import get_user_model
        from django.contrib.auth.password_validation import validate_password
        User=get_user_model()
        if not User.objects.filter(username=username).exists():
            user=User(username=username,email=os.environ.get('BOOTSTRAP_ADMIN_EMAIL',''),is_staff=True,is_superuser=True)
            validate_password(password,user);user.set_password(password);user.save()
            print('管理员账号已创建。',flush=True)
    call_command('clearsessions',verbosity=0)
except Exception:
    # Driver errors may contain host/user information; don't print exceptions with connection strings.
    print('启动未完成：请核对 Render 环境变量、Neon 数据库状态，以及上方迁移日志。',file=sys.stderr,flush=True)
    sys.exit(1)
finally:
    connections.close_all()
os.execvp('gunicorn',['gunicorn','config.wsgi:application','--config','deploy/gunicorn.conf.py'])
