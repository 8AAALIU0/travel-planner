"""Parse Neon URLs without logging credentials; pooled connections use no named cursors."""
from urllib.parse import urlsplit, unquote, parse_qsl
from django.core.exceptions import ImproperlyConfigured

def parse_database_url(value):
    error='DATABASE_URL 必须填写 Neon 提供的完整 PostgreSQL 连接串，不要带 psql 命令或引号。'
    try:
        u=urlsplit(value)
        if u.scheme not in ('postgres','postgresql') or not u.hostname or not u.username or not u.password or not u.path.strip('/') or u.fragment:
            raise ValueError()
        options=dict(parse_qsl(u.query,strict_parsing=True))
        if set(options)-{'sslmode','channel_binding','connect_timeout','application_name'}:raise ValueError()
        if options.get('sslmode','require') not in ('require','verify-ca','verify-full'):raise ValueError()
        if options.get('channel_binding','require') not in ('prefer','require'):raise ValueError()
        options.setdefault('sslmode','require')
        options.setdefault('channel_binding','require')
        options['connect_timeout']=15
        return {'ENGINE':'django.db.backends.postgresql','NAME':unquote(u.path[1:]),
                'USER':unquote(u.username),'PASSWORD':unquote(u.password),
                'HOST':u.hostname,'PORT':u.port or 5432,'OPTIONS':options,
                'CONN_MAX_AGE':0,'CONN_HEALTH_CHECKS':True,
                'DISABLE_SERVER_SIDE_CURSORS':True}
    except (ValueError,TypeError):raise ImproperlyConfigured(error) from None
