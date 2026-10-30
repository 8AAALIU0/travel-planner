"""Render entry point. No GUI prompts or root-only secrets files."""
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
os.environ['DJANGO_SETTINGS_MODULE']='config.render_settings'
import django
django.setup()
from django.core.management import call_command
from django.db import connections,transaction
from travel.models import Attraction, AmapRecord
from django.core import serializers
from django.core.management.color import no_style
try:
    call_command('check',deploy=True,fail_level='WARNING')
    call_command('migrate',interactive=False,verbosity=1)
    print('数据库迁移完成，检查初始景点资料……', flush=True)
    # Batch inserts avoid thousands of database round trips during first startup.
    # Never merge a fixture into an already populated user database.
    if not Attraction.objects.exists():
        fixture = Path(__file__).resolve().parent.parent / 'travel/fixtures/initial_attractions.json'
        groups = {Attraction: [], AmapRecord: []}
        for item in serializers.deserialize('json', fixture.read_text(encoding='utf-8')):
            model = type(item.object)
            if model not in groups or item.m2m_data:
                raise ValueError('初始资料包含不支持的记录类型。')
            groups[model].append(item.object)
        with transaction.atomic():
            # A failed attempt rolls back both tables and can safely be retried.
            for model, objects in groups.items():
                print(f'正在批量导入 {model.__name__}：共 {len(objects)} 条……', flush=True)
                for offset in range(0, len(objects), 100):
                    model.objects.bulk_create(objects[offset:offset + 100], batch_size=100)
                    print(f'{model.__name__}：{min(offset + 100, len(objects))}/{len(objects)}', flush=True)
            # Explicit fixture IDs must not collide with future inserted records.
            with connections['default'].cursor() as cursor:
                for statement in connections['default'].ops.sequence_reset_sql(no_style(), list(groups)):
                    cursor.execute(statement)
        print('初始景点资料导入完成。', flush=True)
    else:
        print('数据库已有景点资料，保留现有数据，跳过初始导入。', flush=True)
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
print('初始化完成，正在启动网站服务……', flush=True)
os.execvp('gunicorn',['gunicorn','config.wsgi:application','--config','deploy/gunicorn.conf.py'])
