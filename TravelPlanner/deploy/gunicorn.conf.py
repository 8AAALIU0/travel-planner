import os
bind='0.0.0.0:'+os.environ.get('PORT','10000')
workers=1
worker_class='gthread'
threads=4
timeout=600
graceful_timeout=60
keepalive=5
accesslog=None
errorlog='-'
loglevel='warning'
forwarded_allow_ips='*'
