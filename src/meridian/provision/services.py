"""nginx and connection page provisioning steps.

nginx handles both SNI-based TCP routing (stream module) and TLS termination
+ web serving (http module), replacing the previous HAProxy + Caddy setup.
Certificate management via acme.sh.
"""

from __future__ import annotations

import re
import shlex
import textwrap
import time
import urllib.parse
from typing import TypeVar

from meridian.config import (
    ACME_SERVER,
    DEFAULT_BRAND_COLOR,
    DEFAULT_BRAND_NAME,
    DEFAULT_FINGERPRINT,
    DEFAULT_PANEL_PORT,
    DEFAULT_PROFILE_NAME,
    DEFAULT_SERVER_ICON,
    TELEGRAM_ADMIN_ID,
    TELEGRAM_BOT_TOKEN,
)
from meridian.provision.steps import ProvisionContext, StepResult
from meridian.ssh import ServerConnection

# ---------------------------------------------------------------------------
# nginx stream configuration (SNI routing — replaces HAProxy)
# ---------------------------------------------------------------------------


def _render_nginx_stream_config(
    reality_sni: str,
    reality_backend_port: int,
    nginx_internal_port: int,
    server_ip: str = "",
    domain: str = "",
) -> str:
    """Render the nginx stream configuration for SNI-based routing.

    nginx stream sits on port 443 and inspects the TLS ClientHello SNI
    WITHOUT terminating TLS. Reality-targeted SNIs go to Xray, server
    IP/domain/no-SNI go to nginx HTTPS (connection pages).
    Unknown SNIs are TCP-proxied to the Reality dest site — a censor
    probing with SNI=google.com sees the dest site's real cert, not
    nginx's, eliminating the SNI routing differential.
    """
    # Build SNI → backend map entries
    map_entries = [
        # Per-relay SNI entries are included from individual files.
        # Each relay gets its own map file created during relay deploy.
        "    include /etc/nginx/stream.d/relay-maps/*.conf;",
        f"    {reality_sni}  xray_reality;",
    ]
    if server_ip:
        map_entries.append(f"    {server_ip}  nginx_https;")
    if domain:
        map_entries.append(f"    {domain}  nginx_https;")

    # No SNI (browsers connecting to bare IP per RFC 6066) → nginx
    # (needed for connection pages accessed via https://<IP>/...)
    map_entries.append('    ""  nginx_https;')
    # Unknown SNI → proxy to Reality dest (eliminates SNI differential —
    # censor probing with random SNIs sees the dest site, not nginx)
    map_entries.append("    default  reality_dest;")

    map_block = "\n".join(map_entries)

    # Flow comment lines
    flow_lines = [
        f"SNI={reality_sni} -> Xray Reality (127.0.0.1:{reality_backend_port})",
    ]
    if server_ip:
        flow_lines.append(f"SNI={server_ip} -> nginx HTTPS (127.0.0.1:{nginx_internal_port})")
    if domain:
        flow_lines.append(f"SNI={domain} -> nginx HTTPS (127.0.0.1:{nginx_internal_port})")
    flow_lines.append(f"No SNI (bare IP) -> nginx HTTPS (127.0.0.1:{nginx_internal_port})")
    flow_lines.append(f"Unknown SNI -> TCP proxy to {reality_sni}:443 (no differential)")
    flow_comment = "\n".join(f"#   {line}" for line in flow_lines)

    return textwrap.dedent(f"""\
        # nginx SNI Router (stream module)
        # Managed by Meridian. Manual edits will be overwritten on next deploy.
        #
        # Flow:
        {flow_comment}

        map_hash_bucket_size 128;

        map $ssl_preread_server_name $meridian_backend {{
        {map_block}
        }}

        upstream xray_reality {{
            server 127.0.0.1:{reality_backend_port};
        }}

        upstream nginx_https {{
            server 127.0.0.1:{nginx_internal_port};
        }}

        upstream reality_dest {{
            server {reality_sni}:443;
        }}

        server {{
            listen 443;
            ssl_preread on;
            proxy_pass $meridian_backend;
            # Short timeout — don't wait 60s (default) if a backend is
            # temporarily unavailable.
            proxy_connect_timeout 1s;
            # VPN sessions can idle for extended periods (user not browsing).
            # Default 10m kills these; 30m is more forgiving while still
            # reclaiming truly dead connections.
            proxy_timeout 30m;
            # TCP keepalives prevent NATs/firewalls from dropping idle
            # connections — critical for relay→exit paths.
            proxy_socket_keepalive on;
        }}
    """)


# ---------------------------------------------------------------------------
# nginx http configuration (TLS + reverse proxy + web — replaces Caddy)
# ---------------------------------------------------------------------------


def _render_xhttp_location(xhttp_path: str) -> str:
    """Render the XHTTP reverse proxy location block."""
    return textwrap.dedent(f"""\

        # --- VLESS+XHTTP (enhanced stealth, nginx-terminated TLS) ---
        # Xray expects the canonical path without a trailing slash, but some
        # clients/browsers probe both forms. Route both to the same upstream.
        location = /{xhttp_path} {{
            proxy_pass http://meridian_xhttp;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
            proxy_buffering off;
            proxy_request_buffering off;
        }}

        # Long timeouts: XHTTP mode=auto lets clients negotiate streaming
        # modes (stream-one/stream-up) with long-lived connections.
        location /{xhttp_path}/ {{
            proxy_pass http://meridian_xhttp;
            proxy_http_version 1.1;
            # Empty Connection header enables upstream keepalive reuse —
            # without this, nginx sends Connection: close per request.
            proxy_set_header Connection "";
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
            proxy_buffering off;
            proxy_request_buffering off;
        }}
    """).rstrip()


def _render_xhttp_upstream(xhttp_internal_port: int) -> str:
    """Render the XHTTP upstream keepalive pool block."""
    return textwrap.dedent(f"""\
        upstream meridian_xhttp {{
            server 127.0.0.1:{xhttp_internal_port};
            keepalive 32;
            keepalive_requests 10000;
            keepalive_timeout 300s;
        }}
    """)


def _render_nginx_http_config(
    domain: str,
    nginx_internal_port: int,
    ws_path: str,
    wss_internal_port: int,
    panel_web_base_path: str,
    panel_internal_port: int,
    info_page_path: str,
    xhttp_path: str = "",
    xhttp_internal_port: int = 0,
) -> str:
    """Render the nginx http configuration for domain mode.

    Architecture: nginx stream (port 443) -> nginx http (internal port)
    nginx stream does SNI routing without TLS termination.
    nginx http handles TLS with certificates issued by acme.sh.
    """
    wss_block = textwrap.dedent(f"""\

        # --- VLESS+WSS Fallback (Cloudflare CDN path) ---
        location /{ws_path} {{
            proxy_pass http://127.0.0.1:{wss_internal_port};
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_read_timeout 360s;
        }}
    """).rstrip()

    xhttp_block = ""
    xhttp_upstream = ""
    if xhttp_path and xhttp_internal_port > 0:
        xhttp_block = _render_xhttp_location(xhttp_path)
        xhttp_upstream = _render_xhttp_upstream(xhttp_internal_port)

    # Root: nginx's built-in 403 page. NOT a custom Meridian page — custom
    # HTML would be fingerprintable (one known server reveals all others).
    # nginx generates 403/404 bodies itself, identical across all installs.
    root_action = "return 403;"
    default_action = "return 404;"

    return _render_nginx_server_block(
        host=domain,
        nginx_internal_port=nginx_internal_port,
        panel_web_base_path=panel_web_base_path,
        panel_internal_port=panel_internal_port,
        info_page_path=info_page_path,
        extra_locations=wss_block + xhttp_block,
        upstream_blocks=xhttp_upstream,
        root_action=root_action,
        default_action=default_action,
        mode_comment="Domain Mode",
        tls_comment=(f"TLS: certificates issued by acme.sh for {domain}"),
        redirect_http=True,
    )


def _render_nginx_ip_config(
    server_ip: str,
    nginx_internal_port: int,
    panel_web_base_path: str,
    panel_internal_port: int,
    info_page_path: str,
    xhttp_path: str = "",
    xhttp_internal_port: int = 0,
) -> str:
    """Render nginx http configuration for IP certificate mode (no domain).

    Architecture: nginx stream (port 443) -> nginx http (internal port)
    TLS via Let's Encrypt IP certificate (acme.sh --certificate-profile shortlived).
    """
    xhttp_block = ""
    xhttp_upstream = ""
    if xhttp_path and xhttp_internal_port > 0:
        xhttp_block = _render_xhttp_location(xhttp_path)
        xhttp_upstream = _render_xhttp_upstream(xhttp_internal_port)

    # Root: nginx's built-in 403 — see domain mode comment for rationale.
    root_action = "return 403;"
    default_action = "return 404;"

    return _render_nginx_server_block(
        host=server_ip,
        nginx_internal_port=nginx_internal_port,
        panel_web_base_path=panel_web_base_path,
        panel_internal_port=panel_internal_port,
        info_page_path=info_page_path,
        extra_locations=xhttp_block,
        upstream_blocks=xhttp_upstream,
        root_action=root_action,
        default_action=default_action,
        mode_comment="IP Certificate Mode",
        tls_comment=("TLS: Let's Encrypt IP certificate (acme.sh, shortlived profile)"),
        redirect_http=False,
    )


def _render_nginx_server_block(
    host: str,
    nginx_internal_port: int,
    panel_web_base_path: str,
    panel_internal_port: int,
    info_page_path: str,
    extra_locations: str,
    root_action: str,
    default_action: str,
    mode_comment: str,
    tls_comment: str,
    redirect_http: bool = True,
    upstream_blocks: str = "",
) -> str:
    """Render the shared nginx server block structure.

    Used by both domain and IP config renderers to avoid duplication.
    redirect_http: True = HTTP→HTTPS redirect (domain mode, has real content).
                   False = ACME-only, no redirect (IP mode — redirect to
                   HTTPS that returns 403 is a contradiction signal).
    """
    csp = "default-src 'self'; img-src 'self' data:; connect-src 'self'"

    # Port 80 behavior: domain mode redirects (has real content),
    # IP mode serves ACME challenges only (no redirect — redirect to
    # HTTPS that returns 403 is a contradiction signal for censors).
    if redirect_http:
        http_default = "return 301 https://$host$request_uri;"
    else:
        http_default = "return 403;"

    return textwrap.dedent(f"""\
        # Meridian Proxy Configuration ({mode_comment})
        # Managed by Meridian — this file is overwritten on each deploy.
        #
        # Architecture: nginx stream (port 443) -> nginx http (port {nginx_internal_port})
        # {tls_comment}

        # --- Cache control for connection pages (map avoids add_header inheritance) ---
        map $uri $meridian_cache {{
            ~*/pwa/            "public, max-age=86400";
            ~*/config\\.json$   "no-cache, must-revalidate";
            ~*/sub\\.txt$       "no-cache, must-revalidate";
            ~*/stats/          "no-cache, must-revalidate";
            default            "no-store";
        }}

        map $uri $meridian_sw {{
            ~*/sw\\.js$   "/";
            default      "";
        }}

        # WebSocket upgrade: only set Connection: upgrade when client sends Upgrade header
        map $http_upgrade $connection_upgrade {{
            default upgrade;
            ""      close;
        }}
    {upstream_blocks}
        server {{
            listen 127.0.0.1:{nginx_internal_port} ssl;
            http2 on;
            server_name {host};
            server_tokens off;

            ssl_certificate     /etc/ssl/meridian/fullchain.pem;
            ssl_certificate_key /etc/ssl/meridian/key.pem;
            ssl_protocols TLSv1.2 TLSv1.3;
    {extra_locations}

            # --- 3x-ui Panel (management interface on secret path) ---
            location /{panel_web_base_path}/ {{
                proxy_pass http://127.0.0.1:{panel_internal_port};
                proxy_http_version 1.1;
                proxy_set_header Host $host;
                proxy_set_header Upgrade $http_upgrade;
                proxy_set_header Connection $connection_upgrade;
            }}

            # --- Connection Info Pages (PWA with per-client config) ---
            # alias strips the location prefix (like Caddy's handle_path).
            location /{info_page_path}/ {{
                alias /var/www/private/;

                add_header Cache-Control $meridian_cache always;
                add_header Service-Worker-Allowed $meridian_sw always;
                add_header Content-Security-Policy "{csp}" always;
                add_header X-Content-Type-Options "nosniff" always;
                add_header X-Frame-Options "DENY" always;
                add_header Referrer-Policy "no-referrer" always;
            }}

            # Root: nginx-generated 403 (not custom HTML — avoids fingerprinting)
            location = / {{
                {root_action}
            }}

            # Default: nginx-generated 404
            location / {{
                {default_action}
            }}

            access_log /var/log/nginx/meridian.log;
        }}

        # --- HTTP: ACME challenge{" + redirect" if redirect_http else " only (no redirect)"} ---
        server {{
            listen 80;
            server_name {host};
            server_tokens off;

            location /.well-known/acme-challenge/ {{
                root /var/www/acme;
            }}

            location / {{
                {http_default}
            }}
        }}
    """)


# ---------------------------------------------------------------------------
# Stats update script template
# ---------------------------------------------------------------------------


def _render_stats_script(panel_internal_port: int) -> str:
    """Render the stats update Python script."""
    from meridian.protocols import INBOUND_TYPES

    prefixes_repr = repr({it.email_prefix: key for key, it in INBOUND_TYPES.items()})
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        \"\"\"Fetch per-client traffic stats from 3x-ui and write per-client JSON files.

        Each client gets a stats file named by their Reality UUID -- the same UUID
        that appears in their VLESS connection URL. Only someone with the URL can
        find their stats file. Runs via cron every 5 minutes.
        \"\"\"
        import json, urllib.request, urllib.parse, http.cookiejar, os, time, sys

        CREDS = '/etc/meridian/proxy.yml'
        STATS_DIR = '/var/www/private/stats'
        PREFIXES = {prefixes_repr}

        def parse_creds():
            \"\"\"Parse v2 nested YAML credentials.\"\"\"
            import json as _json
            creds = {{}}
            with open(CREDS) as f:
                content = f.read()
            try:
                import importlib
                _yaml = importlib.import_module('yaml')
                data = _yaml.safe_load(content)
                if isinstance(data, dict):
                    panel = data.get('panel', {{}})
                    creds['panel_username'] = panel.get('username', '')
                    creds['panel_password'] = panel.get('password', '')
                    creds['panel_web_base_path'] = panel.get('web_base_path', '')
            except ImportError:
                with open(CREDS) as f:
                    for line in f:
                        line = line.strip()
                        if ':' in line and not line.startswith('#') and not line.startswith('-'):
                            key, val = line.split(':', 1)
                            creds[key.strip()] = val.strip().strip('"')
            return creds

        def main():
            creds = parse_creds()
            wbp = creds.get('panel_web_base_path', '')
            base = f"http://127.0.0.1:{panel_internal_port}/{{wbp}}"

            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            login_data = "username={{0}}&password={{1}}".format(
                urllib.parse.quote(creds.get('panel_username', ''), safe=''),
                urllib.parse.quote(creds.get('panel_password', ''), safe=''),
            ).encode()
            try:
                opener.open(urllib.request.Request(f"{{base}}/login", data=login_data, method='POST'))
            except Exception:
                sys.exit(1)

            try:
                resp = opener.open(f"{{base}}/panel/api/inbounds/list")
                inbounds = json.load(resp)
            except Exception:
                sys.exit(1)

            if not inbounds.get('success'):
                sys.exit(1)

            clients = {{}}
            for inbound in inbounds.get('obj', []):
                settings = json.loads(inbound['settings'])
                for client in settings.get('clients', []):
                    email = client['email']
                    uuid = client['id']
                    for prefix, proto_key in PREFIXES.items():
                        if email.startswith(prefix):
                            name = email[len(prefix):]
                            clients.setdefault(name, {{}})
                            if proto_key == 'reality':
                                clients[name]['reality_uuid'] = uuid
                            clients[name].setdefault('emails', []).append(email)
                            break

            os.makedirs(STATS_DIR, exist_ok=True)

            active_uuids = set()
            for name, info in clients.items():
                uuid = info.get('reality_uuid')
                if not uuid:
                    continue
                active_uuids.add(uuid)

                total_up = 0
                total_down = 0
                last_online = 0

                for email in info.get('emails', []):
                    try:
                        resp = opener.open(f"{{base}}/panel/api/inbounds/getClientTraffics/{{email}}")
                        data = json.load(resp)
                        if data.get('success') and data.get('obj'):
                            obj = data['obj']
                            total_up += obj.get('up', 0)
                            total_down += obj.get('down', 0)
                            lo = obj.get('lastOnline', 0)
                            if lo > last_online:
                                last_online = lo
                    except Exception:
                        pass

                stats = {{
                    'up': total_up,
                    'down': total_down,
                    'total': total_up + total_down,
                    'lastOnline': last_online,
                    'updated': int(time.time() * 1000)
                }}
                path = os.path.join(STATS_DIR, f"{{uuid}}.json")
                with open(path, 'w') as f:
                    json.dump(stats, f)
                os.chmod(path, 0o644)

            for fname in os.listdir(STATS_DIR):
                if fname.endswith('.json'):
                    uid = fname[:-5]
                    if uid not in active_uuids:
                        os.remove(os.path.join(STATS_DIR, fname))

        if __name__ == '__main__':
            main()
    """)


# ---------------------------------------------------------------------------
# InstallNginx — install nginx binary, stream module, and acme.sh
# ---------------------------------------------------------------------------


class InstallNginx:
    """Install nginx, stream module, and acme.sh.

    Handles upgrade path from old HAProxy+Caddy stack, version
    requirements (>=1.16), and the nginx.org official repo fallback.
    """

    name = "Install nginx"

    def __init__(self, email: str = "") -> None:
        self.email = email

    def run(self, conn: ServerConnection, ctx: ProvisionContext) -> StepResult:
        changed = False

        # -- Upgrade path: stop old HAProxy and Caddy if present --
        conn.run(
            "systemctl stop haproxy 2>/dev/null; systemctl disable haproxy 2>/dev/null; true",
            timeout=15,
        )
        conn.run(
            "systemctl stop caddy 2>/dev/null; systemctl disable caddy 2>/dev/null; true",
            timeout=15,
        )
        # Remove old watchdog immediately to prevent it from restarting
        # haproxy/caddy during the deploy (cron runs every 5 min)
        conn.run("rm -f /etc/meridian/health-check.sh", timeout=15)
        # Clean up old config files and cert storage
        conn.run(
            "rm -f /etc/haproxy/haproxy.cfg /etc/caddy/conf.d/meridian.caddy /etc/caddy/Caddyfile && "
            "rm -rf /etc/systemd/system/haproxy.service.d /etc/systemd/system/caddy.service.d "
            "/var/lib/caddy/.local/share/caddy && "
            "systemctl daemon-reload 2>/dev/null; true",
            timeout=15,
        )

        # -- Check if nginx is already installed and meets version requirement --
        check = conn.run("dpkg -l nginx 2>/dev/null | grep -q '^ii'", timeout=15)
        already_installed = check.returncode == 0
        needs_official_repo = False

        if already_installed:
            ver_check = conn.run("nginx -v 2>&1", timeout=15)
            ver_output = ver_check.stdout + ver_check.stderr
            m = re.search(r"nginx/(\d+)\.(\d+)", ver_output)
            if m and (int(m.group(1)), int(m.group(2))) < (1, 25):
                needs_official_repo = True
        else:
            # Not installed — try distro repo first, upgrade if too old
            result = conn.run(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y nginx",
                timeout=180,
            )
            if result.returncode != 0:
                # Distro install failed — fall through to official repo
                needs_official_repo = True
            else:
                changed = True
                ver_check = conn.run("nginx -v 2>&1", timeout=15)
                ver_output = ver_check.stdout + ver_check.stderr
                m = re.search(r"nginx/(\d+)\.(\d+)", ver_output)
                if m and (int(m.group(1)), int(m.group(2))) < (1, 25):
                    needs_official_repo = True

        if needs_official_repo:
            # Install from official nginx.org repo (mirrors Docker pattern)
            distro = conn.run("bash -c '. /etc/os-release && echo $ID'", timeout=15)
            distro_name = distro.stdout.strip().lower() if distro.returncode == 0 else "ubuntu"

            codename = conn.run("bash -c '. /etc/os-release && echo $VERSION_CODENAME'", timeout=15)
            distro_codename = codename.stdout.strip() if codename.returncode == 0 else "jammy"

            # Remove conflicting distro packages before official repo install
            conn.run(
                "DEBIAN_FRONTEND=noninteractive apt-get remove -y"
                " nginx-common nginx-core nginx-full 'libnginx-mod-*' 2>/dev/null; true",
                timeout=120,
            )

            # Ensure keyrings directory exists (missing on Ubuntu < 22.04)
            conn.run("mkdir -p /etc/apt/keyrings && chmod 755 /etc/apt/keyrings", timeout=15)

            # Add nginx.org signing key
            result = conn.run(
                "curl -fsSL https://nginx.org/keys/nginx_signing.key"
                " -o /etc/apt/keyrings/nginx.asc"
                " && chmod 644 /etc/apt/keyrings/nginx.asc",
                timeout=60,
            )
            if result.returncode != 0:
                return StepResult(
                    name=self.name,
                    status="failed",
                    detail=f"Failed to add nginx signing key: {result.stderr.strip()[:200]}",
                )

            # Add nginx.org stable repo
            repo_line = (
                f"deb [signed-by=/etc/apt/keyrings/nginx.asc] "
                f"https://nginx.org/packages/{distro_name} "
                f"{distro_codename} nginx"
            )
            conn.run(
                f"echo {shlex.quote(repo_line)} > /etc/apt/sources.list.d/nginx-official.list",
                timeout=15,
            )

            # Pin official nginx packages higher to override distro
            conn.run(
                "printf 'Package: nginx*\\nPin: origin nginx.org\\n"
                "Pin-Priority: 900\\n' > /etc/apt/preferences.d/99nginx",
                timeout=15,
            )

            result = conn.run(
                "DEBIAN_FRONTEND=noninteractive apt-get update -qq"
                " && DEBIAN_FRONTEND=noninteractive apt-get install -y"
                " -o Dpkg::Options::=--force-confnew nginx",
                timeout=180,
            )
            if result.returncode != 0:
                return StepResult(
                    name=self.name,
                    status="failed",
                    detail=f"Failed to install nginx from official repo: {result.stderr.strip()[:200]}",
                )

            changed = True

            # Clean up stale load_module directives — official nginx has
            # stream compiled statically, old distro nginx.conf may reference
            # dynamic .so files that no longer exist.
            conn.run(
                "sed -i '/load_module.*ngx_stream_module/d' /etc/nginx/nginx.conf 2>/dev/null; true",
                timeout=15,
            )

        # -- Ensure stream module is available --
        conn.run(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libnginx-mod-stream 2>/dev/null; true",
            timeout=120,
        )
        check = conn.run(
            "test -f /usr/lib/nginx/modules/ngx_stream_module.so || nginx -V 2>&1 | grep -q 'with-stream '",
            timeout=15,
        )
        if check.returncode != 0:
            return StepResult(
                name=self.name,
                status="failed",
                detail="nginx stream module not available — install libnginx-mod-stream",
            )

        # -- Create directories --
        conn.run(
            "mkdir -p /var/www/private /var/www/acme/.well-known/acme-challenge "
            "/etc/ssl/meridian /etc/nginx/stream.d /etc/nginx/stream.d/relay-maps && "
            "chown -R www-data:www-data /var/www/private /var/www/acme",
            timeout=15,
        )

        # -- Ensure webmanifest MIME type is registered --
        conn.run(
            "grep -q webmanifest /etc/nginx/mime.types || "
            r"sed -i '/^}/i \    application/manifest+json  webmanifest;' /etc/nginx/mime.types",
            timeout=15,
        )

        # -- Install acme.sh (if not already installed) --
        check = conn.run("test -f /root/.acme.sh/acme.sh", timeout=15)
        if check.returncode != 0:
            # email='' breaks acme.sh installer (shift error), omit when empty
            email_flag = f"email={shlex.quote(self.email)}" if self.email else ""
            result = conn.run(
                f"curl -fsSL https://get.acme.sh | sh -s -- {email_flag}",
                timeout=120,
            )
            if result.returncode != 0:
                return StepResult(
                    name=self.name,
                    status="failed",
                    detail=f"Failed to install acme.sh: {result.stderr.strip()}",
                )
            changed = True

        cron_check = conn.run("crontab -l 2>/dev/null | grep -q 'acme.sh --cron'", timeout=15)
        result = conn.run("/root/.acme.sh/acme.sh --install-cronjob", timeout=60)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            return StepResult(
                name=self.name,
                status="failed",
                detail=f"Failed to install acme.sh cron job: {detail[:200]}",
            )
        if cron_check.returncode != 0:
            changed = True

        return StepResult(name=self.name, status="changed" if changed else "ok")


# ---------------------------------------------------------------------------
# ConfigureNginx — deploy configs, validate, start/reload
# ---------------------------------------------------------------------------


_T = TypeVar("_T")


def _resolve_ctx(val: _T | None, fallback: _T) -> _T:
    """Resolve a constructor value with context fallback.

    None = "not provided by caller, use context". Explicit values
    (including falsy ones like 0 or "") are respected as-is.
    """
    return val if val is not None else fallback


class ConfigureNginx:
    """Deploy nginx stream + http configs, validate, and start/reload.

    Reads context values set by ConfigurePanel for paths and ports.
    """

    name = "Configure nginx"

    def __init__(
        self,
        domain: str,
        reality_sni: str | None = None,
        reality_backend_port: int | None = None,
        nginx_internal_port: int = 8443,
        ws_path: str | None = None,
        wss_internal_port: int | None = None,
        panel_web_base_path: str | None = None,
        panel_internal_port: int | None = None,
        info_page_path: str | None = None,
        server_ip: str | None = None,
        skip_dns_check: bool = False,
        ip_mode: bool = False,
        xhttp_path: str | None = None,
        xhttp_internal_port: int | None = None,
    ) -> None:
        self.domain = domain
        self.reality_sni = reality_sni
        self.reality_backend_port = reality_backend_port
        self.nginx_internal_port = nginx_internal_port
        self.ws_path = ws_path
        self.wss_internal_port = wss_internal_port
        self.panel_web_base_path = panel_web_base_path
        self.panel_internal_port = panel_internal_port
        self.info_page_path = info_page_path
        self.server_ip = server_ip
        self.skip_dns_check = skip_dns_check
        self.ip_mode = ip_mode
        self.xhttp_path = xhttp_path
        self.xhttp_internal_port = xhttp_internal_port

    def run(self, conn: ServerConnection, ctx: ProvisionContext) -> StepResult:
        # Resolve runtime values from context (populated by ConfigurePanel).
        panel_web_base_path = _resolve_ctx(self.panel_web_base_path, ctx.get("web_base_path", ""))
        info_page_path = _resolve_ctx(self.info_page_path, ctx.get("info_page_path", ""))
        panel_internal_port = _resolve_ctx(self.panel_internal_port, ctx.panel_port)
        server_ip = _resolve_ctx(self.server_ip, ctx.ip)
        xhttp_path = _resolve_ctx(self.xhttp_path, ctx.get("xhttp_path", ""))
        xhttp_internal_port = _resolve_ctx(
            self.xhttp_internal_port,
            ctx.xhttp_port if ctx.xhttp_enabled else 0,
        )
        ws_path = _resolve_ctx(self.ws_path, ctx.get("ws_path", ""))
        wss_internal_port = _resolve_ctx(self.wss_internal_port, ctx.wss_port)
        reality_sni = _resolve_ctx(self.reality_sni, ctx.sni)
        reality_backend_port = _resolve_ctx(self.reality_backend_port, ctx.reality_port)

        # -- DNS pre-check (domain mode only) --
        if not self.ip_mode and not self.skip_dns_check:
            dns_result = _check_domain_dns(conn, self.domain, server_ip)
            if dns_result is not None:
                return StepResult(name=self.name, status="failed", detail=dns_result)

        # -- Bootstrap: generate self-signed cert so nginx can start --
        check = conn.run("test -f /etc/ssl/meridian/fullchain.pem", timeout=15)
        if check.returncode != 0:
            cert_host = server_ip if self.ip_mode else self.domain
            q_subj = shlex.quote(f"/CN={cert_host}")
            san_ext = shlex.quote(f"subjectAltName=DNS:{cert_host}")
            if self.ip_mode:
                san_ext = shlex.quote(f"subjectAltName=IP:{cert_host}")
            result = conn.run(
                f"openssl req -x509 -newkey rsa:2048 -keyout /etc/ssl/meridian/key.pem "
                f"-out /etc/ssl/meridian/fullchain.pem -days 1 -nodes "
                f"-subj {q_subj} -addext {san_ext}",
                timeout=15,
            )
            if result.returncode != 0:
                return StepResult(
                    name=self.name,
                    status="failed",
                    detail="Failed to generate bootstrap certificate",
                )

        # -- Deploy nginx stream config --
        stream_config = _render_nginx_stream_config(
            reality_sni=reality_sni,
            reality_backend_port=reality_backend_port,
            nginx_internal_port=self.nginx_internal_port,
            server_ip=server_ip,
            domain=self.domain,
        )
        q_stream = shlex.quote(stream_config)
        result = conn.run(
            f"printf '%s' {q_stream} > /etc/nginx/stream.d/meridian.conf",
            timeout=15,
        )
        if result.returncode != 0:
            return StepResult(
                name=self.name,
                status="failed",
                detail=f"Failed to write stream config: {result.stderr.strip()}",
            )

        # -- Deploy nginx http config --
        if self.ip_mode:
            http_config = _render_nginx_ip_config(
                server_ip=server_ip,
                nginx_internal_port=self.nginx_internal_port,
                panel_web_base_path=panel_web_base_path,
                panel_internal_port=panel_internal_port,
                info_page_path=info_page_path,
                xhttp_path=xhttp_path,
                xhttp_internal_port=xhttp_internal_port,
            )
        else:
            http_config = _render_nginx_http_config(
                domain=self.domain,
                nginx_internal_port=self.nginx_internal_port,
                ws_path=ws_path,
                wss_internal_port=wss_internal_port,
                panel_web_base_path=panel_web_base_path,
                panel_internal_port=panel_internal_port,
                info_page_path=info_page_path,
                xhttp_path=xhttp_path,
                xhttp_internal_port=xhttp_internal_port,
            )
        q_http = shlex.quote(http_config)
        result = conn.run(
            f"printf '%s' {q_http} > /etc/nginx/conf.d/meridian-http.conf",
            timeout=15,
        )
        if result.returncode != 0:
            return StepResult(
                name=self.name,
                status="failed",
                detail=f"Failed to write http config: {result.stderr.strip()}",
            )

        # -- Ensure nginx.conf has a stream block --
        check = conn.run("grep -q 'stream {' /etc/nginx/nginx.conf", timeout=15)
        if check.returncode != 0:
            stream_block = "\\nstream {\\n    include /etc/nginx/stream.d/*.conf;\\n}\\n"
            conn.run(
                f"printf '{stream_block}' >> /etc/nginx/nginx.conf",
                timeout=15,
            )

        # -- Remove default site (conflicts with our port 80 listener) --
        conn.run("rm -f /etc/nginx/sites-enabled/default", timeout=15)

        # -- Validate configuration --
        result = conn.run("nginx -t 2>&1", timeout=15)
        if result.returncode != 0:
            return StepResult(
                name=self.name,
                status="failed",
                detail=f"nginx config validation failed: {result.stderr.strip() or result.stdout.strip()}",
            )

        # -- Ensure nginx restarts on failure --
        conn.run(
            "mkdir -p /etc/systemd/system/nginx.service.d && "
            "printf '[Service]\\nRestart=on-failure\\nRestartSec=5\\n' "
            "> /etc/systemd/system/nginx.service.d/restart.conf && "
            "systemctl daemon-reload",
            timeout=15,
        )

        # -- Start/enable/reload nginx --
        conn.run("systemctl enable nginx", timeout=15)
        result = conn.run("systemctl reload-or-restart nginx", timeout=30)
        if result.returncode != 0:
            return StepResult(
                name=self.name,
                status="failed",
                detail=f"Failed to start nginx: {result.stderr.strip()}",
            )

        host = server_ip if self.ip_mode else self.domain
        return StepResult(
            name=self.name,
            status="changed",
            detail=f"nginx configured for {host}:{self.nginx_internal_port}",
        )


# ---------------------------------------------------------------------------
# IssueTLSCert — issue real TLS certificate via acme.sh
# ---------------------------------------------------------------------------


_SHORTLIVED_IP_CERT_RENEWAL_DAYS = 5
_SHORTLIVED_IP_CERT_MAX_NEXT_RENEW_SECONDS = 7 * 24 * 60 * 60


def _load_acme_domain_info(conn: ServerConnection, cert_host: str) -> str | None:
    """Return acme.sh domain config output, or None if this host is unknown."""
    q_cert_host = shlex.quote(cert_host)
    result = conn.run(
        f"/root/.acme.sh/acme.sh --info -d {q_cert_host} 2>/dev/null",
        timeout=30,
    )
    if result.returncode != 0 or "Le_Domain=" not in result.stdout:
        return None
    return result.stdout


def _read_acme_int(domain_info: str, key: str) -> int | None:
    """Extract an integer value from acme.sh domain config output."""
    match = re.search(rf"^{re.escape(key)}=['\"]?(\d+)['\"]?$", domain_info, re.MULTILINE)
    return int(match.group(1)) if match else None


def _stale_shortlived_policy(domain_info: str) -> bool:
    """Return True when stored acme renewal metadata is incompatible with 6-day IP certs."""
    renewal_days = _read_acme_int(domain_info, "Le_RenewalDays")
    if renewal_days is not None:
        return renewal_days != _SHORTLIVED_IP_CERT_RENEWAL_DAYS

    next_renew_time = _read_acme_int(domain_info, "Le_NextRenewTime")
    if next_renew_time is None:
        return True

    return next_renew_time > int(time.time()) + _SHORTLIVED_IP_CERT_MAX_NEXT_RENEW_SECONDS


class IssueTLSCert:
    """Issue a real TLS certificate via acme.sh and install it.

    Uses the webroot method against the running nginx. On failure, nginx
    continues running with a self-signed bootstrap cert — Reality VPN
    works regardless since it uses its own encryption.
    """

    name = "Issue TLS certificate"

    def __init__(
        self,
        domain: str,
        ip_mode: bool = False,
        server_ip: str | None = None,
    ) -> None:
        self.domain = domain
        self.ip_mode = ip_mode
        self.server_ip = server_ip

    def run(self, conn: ServerConnection, ctx: ProvisionContext) -> StepResult:
        server_ip = _resolve_ctx(self.server_ip, ctx.ip)
        cert_host = server_ip if self.ip_mode else self.domain
        q_cert_host = shlex.quote(cert_host)
        profile_flag = " --certificate-profile shortlived" if self.ip_mode else ""
        renew_days_flag = ""
        force_flag = ""

        if self.ip_mode:
            renew_days_flag = f" --days {_SHORTLIVED_IP_CERT_RENEWAL_DAYS}"
            domain_info = _load_acme_domain_info(conn, cert_host)
            if domain_info:
                # acme.sh defaults to a 30-day renew window, which is wrong
                # for LE's 6-day IP certs. Force a one-time migration reissue
                # when the stored policy is stale. When the policy is already
                # correct, let acme.sh decide whether to reissue and always
                # re-run --install-cert so teardown/redeploy can reuse the
                # existing cached cert without creating a new order.
                if _stale_shortlived_policy(domain_info):
                    force_flag = " --force"

        result = conn.run(
            f"/root/.acme.sh/acme.sh --issue -d {q_cert_host} "
            f"--webroot /var/www/acme --server {shlex.quote(ACME_SERVER)}"
            f"{profile_flag}{renew_days_flag}{force_flag} 2>&1",
            timeout=180,
        )
        # acme.sh returns 0 on success, 2 if cert already valid (skip renewal)
        cert_issued = result.returncode in (0, 2)

        if cert_issued:
            # Install cert and set reload command for auto-renewal
            install = conn.run(
                f"/root/.acme.sh/acme.sh --install-cert -d {q_cert_host} "
                f"--key-file /etc/ssl/meridian/key.pem "
                f"--fullchain-file /etc/ssl/meridian/fullchain.pem "
                f'--reloadcmd "systemctl reload nginx" 2>&1',
                timeout=60,
            )
            if install.returncode != 0:
                detail = install.stderr.strip() or install.stdout.strip() or "unknown error"
                return StepResult(
                    name=self.name,
                    status="failed",
                    detail=f"Failed to install TLS cert for {cert_host}: {detail[:200]}",
                )
            # Reload to pick up the real cert
            reload = conn.run("systemctl reload nginx", timeout=15)
            if reload.returncode != 0:
                detail = reload.stderr.strip() or reload.stdout.strip() or "unknown error"
                return StepResult(
                    name=self.name,
                    status="failed",
                    detail=f"Failed to reload nginx after TLS cert install for {cert_host}: {detail[:200]}",
                )

        if cert_issued:
            return StepResult(
                name=self.name,
                status="changed",
                detail=f"TLS cert issued for {cert_host}",
            )

        # ACME failed — server runs with self-signed cert.
        # Reality VPN works regardless (own encryption), but connection
        # pages will show browser cert warnings until resolved.
        return StepResult(
            name=self.name,
            status="changed",
            detail=(
                f"WARNING: TLS cert failed for {cert_host} — using self-signed. "
                "Connection pages will show cert warnings. "
                "Check port 80 is open and domain resolves correctly"
            ),
        )


# ---------------------------------------------------------------------------
# DeployPWAAssets
# ---------------------------------------------------------------------------


class DeployPWAAssets:
    """Deploy shared PWA static assets to /var/www/private/pwa/.

    These assets (JS, CSS, service worker, icon) are identical for all
    clients and deployed once.  Per-client files (config.json, manifest,
    index.html, sub.txt) are deployed by DeployConnectionPage.
    """

    name = "Deploy PWA assets"

    def run(self, conn: ServerConnection, ctx: ProvisionContext) -> StepResult:
        from meridian.pwa import upload_pwa_assets

        try:
            error = upload_pwa_assets(conn)
        except Exception as exc:
            return StepResult(
                name=self.name,
                status="failed",
                detail=f"Failed to load PWA assets: {exc}",
            )
        if error:
            return StepResult(
                name=self.name,
                status="failed",
                detail=error,
            )
        return StepResult(
            name=self.name,
            status="changed",
            detail="Shared PWA assets deployed to /var/www/private/pwa/",
        )


# ---------------------------------------------------------------------------
# DeployConnectionPage
# ---------------------------------------------------------------------------


class DeployConnectionPage:
    """Deploy the connection info HTML page and stats infrastructure.

    Generates QR codes on the server using qrencode, deploys the stats update
    script with a cron job, and renders+uploads the connection-info HTML page
    for the default client.

    Reads credentials and config from ProvisionContext (populated by
    ConfigurePanel and earlier steps).
    """

    name = "Deploy connection page"

    def __init__(
        self,
        server_ip: str,
        fingerprint: str = DEFAULT_FINGERPRINT,
    ) -> None:
        self.server_ip = server_ip
        self.fingerprint = fingerprint

    def run(self, conn: ServerConnection, ctx: ProvisionContext) -> StepResult:
        # Read credentials from context (populated by ConfigurePanel)
        creds = ctx.credentials
        if creds is None:
            return StepResult(
                name=self.name,
                status="failed",
                detail="No credentials available — ConfigurePanel may have failed",
            )
        sni = creds.server.sni or ctx.sni
        domain = creds.server.domain or ctx.domain
        reality_uuid = creds.reality.uuid or ""
        reality_public_key = creds.reality.public_key or ""
        reality_short_id = creds.reality.short_id or ""
        wss_uuid = creds.wss.uuid or ""
        ws_path = creds.wss.ws_path or ""
        info_page_path = creds.panel.info_page_path or ctx.get("info_page_path", "")
        panel_internal_port = creds.panel.port or ctx.panel_port
        first_client_name = ctx.get("first_client_name", "default") or "default"
        xhttp_enabled = ctx.xhttp_enabled
        xhttp_path = creds.xhttp.xhttp_path or ctx.get("xhttp_path", "")

        if not reality_uuid:
            return StepResult(
                name=self.name,
                status="failed",
                detail="No Reality UUID found — ConfigurePanel may have failed",
            )

        # Build connection URLs
        encryption = creds.reality.encryption_key or "none"
        reality_url = (
            f"vless://{reality_uuid}@{self.server_ip}:443"
            f"?encryption={encryption}&flow=xtls-rprx-vision"
            f"&security=reality&sni={sni}&fp={self.fingerprint}"
            f"&pbk={reality_public_key}&sid={reality_short_id}"
            f"&type=tcp&headerType=none#VLESS-Reality"
        )

        wss_url = ""
        if domain and wss_uuid and ws_path:
            wss_url = (
                f"vless://{wss_uuid}@{domain}:443"
                f"?encryption=none&security=tls&sni={domain}"
                f"&type=ws&host={domain}&path=%2F{ws_path}#VLESS-WSS-CDN"
            )

        xhttp_url = ""
        if xhttp_enabled and xhttp_path:
            # Use domain if available, otherwise IP
            xhttp_host = domain or self.server_ip
            xhttp_name = f"{DEFAULT_SERVER_ICON} {DEFAULT_PROFILE_NAME}".strip()
            xhttp_url = (
                f"vless://{reality_uuid}@{xhttp_host}:443"
                f"?encryption=none&security=tls&sni={xhttp_host}&fp={self.fingerprint}"
                f"&type=xhttp&path=%2F{xhttp_path}#{urllib.parse.quote(xhttp_name)}"
            )

        # Generate QR codes as base64 PNG (pure Python, no qrencode binary needed)
        from meridian.urls import generate_qr_base64

        reality_qr_b64 = generate_qr_base64(reality_url)
        wss_qr_b64 = generate_qr_base64(wss_url) if wss_url else ""
        xhttp_qr_b64 = generate_qr_base64(xhttp_url) if xhttp_url else ""

        # Store QR data in context for the HTML template
        ctx["reality_qr_b64"] = reality_qr_b64
        ctx["wss_qr_b64"] = wss_qr_b64
        ctx["xhttp_qr_b64"] = xhttp_qr_b64
        ctx["reality_url"] = reality_url
        ctx["wss_url"] = wss_url
        ctx["xhttp_url"] = xhttp_url

        # Deploy stats update script
        stats_script = _render_stats_script(panel_internal_port)
        q_script = shlex.quote(stats_script)
        conn.run("mkdir -p /etc/meridian", timeout=15)
        conn.run(f"printf '%s' {q_script} > /etc/meridian/update-stats.py", timeout=15)
        conn.run("chmod 700 /etc/meridian/update-stats.py", timeout=15)

        # Create stats directory
        conn.run(
            "mkdir -p /var/www/private/stats && chown www-data:www-data /var/www/private/stats",
            timeout=15,
        )

        # Run stats update once
        conn.run("python3 /etc/meridian/update-stats.py", timeout=15)

        # Add cron job (idempotent via crontab manipulation), with syslog logging
        cron_job = "*/5 * * * * python3 /etc/meridian/update-stats.py 2>&1 | logger -t meridian-stats"
        q_cron = shlex.quote(cron_job)
        conn.run(
            f"(crontab -l 2>/dev/null | grep -v 'update-stats.py'; echo {q_cron}) | crontab -",
            timeout=15,
        )

        # Deploy health watchdog cron (checks Xray and nginx every 5 min)
        watchdog_script = (
            "#!/bin/sh\n"
            "# Meridian service health watchdog — restarts crashed services\n"
            "docker exec 3x-ui pgrep -f xray >/dev/null 2>&1 || "
            '{ logger -t meridian-health "Xray not running, restarting 3x-ui"; '
            "docker restart 3x-ui; }\n"
            "systemctl is-active --quiet nginx || "
            '{ logger -t meridian-health "nginx not running, restarting"; '
            "systemctl restart nginx; }\n"
        )
        q_watchdog = shlex.quote(watchdog_script)
        conn.run(f"printf '%s' {q_watchdog} > /etc/meridian/health-check.sh", timeout=15)
        conn.run("chmod 700 /etc/meridian/health-check.sh", timeout=15)

        watchdog_cron = "*/5 * * * * /etc/meridian/health-check.sh 2>&1 | logger -t meridian-health"
        q_wc = shlex.quote(watchdog_cron)
        conn.run(
            f"(crontab -l 2>/dev/null | grep -v 'health-check.sh'; echo {q_wc}) | crontab -",
            timeout=15,
        )

        # Build ProtocolURL list with QR data for connection page
        from meridian.models import ProtocolURL as _PU

        page_urls: list[_PU] = []
        if xhttp_url:
            page_urls.append(_PU(key="xhttp", label="XHTTP", url=xhttp_url, qr_b64=xhttp_qr_b64))

        # Generate and upload PWA per-client files
        from meridian.pwa import generate_client_files, upload_client_files

        host = domain or self.server_ip
        page_url = f"https://{host}/{info_page_path}/{reality_uuid}/"

        client_files = generate_client_files(
            page_urls,
            server_ip=self.server_ip,
            domain=domain,
            client_name=first_client_name,
            server_name=creds.branding.server_name or DEFAULT_BRAND_NAME,
            profile_name=creds.branding.profile_name or DEFAULT_PROFILE_NAME,
            server_icon=creds.branding.icon or DEFAULT_SERVER_ICON,
            color=creds.branding.color or DEFAULT_BRAND_COLOR,
            page_url=page_url,
        )

        upload_error = upload_client_files(conn, reality_uuid, client_files)
        if upload_error:
            return StepResult(
                name=self.name,
                status="failed",
                detail=upload_error,
            )

        ctx["hosted_page_url"] = page_url

        return StepResult(
            name=self.name,
            status="changed",
            detail=f"Connection page live at {page_url}",
        )


# ---------------------------------------------------------------------------
# DeployTelegramBot
# ---------------------------------------------------------------------------


def _render_telegram_bot_script() -> str:
    """Render the self-contained Telegram bot used on deployed servers."""
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import http.cookiejar
        import hashlib
        import json
        import os
        import re
        import sqlite3
        import subprocess
        import time
        import traceback
        import urllib.parse
        import urllib.request
        import uuid
        from pathlib import Path

        CREDS = Path("/etc/meridian/proxy.yml")
        WEB_ROOT = Path("/var/www/private")
        TOKEN = os.environ.get("MERIDIAN_TELEGRAM_BOT_TOKEN", "")
        ADMIN_ID = int(os.environ.get("MERIDIAN_TELEGRAM_ADMIN_ID", "{TELEGRAM_ADMIN_ID}"))
        PROFILE_NAME = "{DEFAULT_PROFILE_NAME}"
        BRAND_NAME = "{DEFAULT_BRAND_NAME}"
        SERVER_ICON = "{DEFAULT_SERVER_ICON}"
        DB_PATH = Path("/etc/meridian/telegram-bot.sqlite3")
        PALLY_API_BASE = os.environ.get("MERIDIAN_PALLY_API_BASE", "https://pally.info/merchant/api").rstrip("/")
        PALLY_API_TOKEN = os.environ.get("MERIDIAN_PALLY_API_TOKEN", "")
        PALLY_SHOP_ID = os.environ.get("MERIDIAN_PALLY_SHOP_ID", "")
        PALLY_SUCCESS_STATUSES = {{"paid", "success", "completed", "confirmed", "done"}}
        TORRENT_LOG_PATHS = [
            Path("/var/log/3x-ui/xray/access.log"),
            Path("/var/log/xray/access.log"),
            Path("/usr/local/x-ui/bin/access.log"),
        ]

        PLANS = {{
            "p_4h_unlim": {{"title": "4 часа безлимита", "price": 10, "days": 0, "hours": 4, "gb": 0}},
            "p_1d_unlim": {{"title": "1 день безлимита", "price": 20, "days": 1, "hours": 0, "gb": 0}},
            "p_7d_unlim": {{"title": "7 дней безлимита", "price": 50, "days": 7, "hours": 0, "gb": 0}},
            "p_1m_100gb": {{"title": "1 месяц 100 ГБ", "price": 100, "days": 30, "hours": 0, "gb": 100}},
            "p_1m_200gb": {{"title": "1 месяц 200 ГБ", "price": 130, "days": 30, "hours": 0, "gb": 200}},
            "p_1m_unlim": {{"title": "1 месяц безлимита", "price": 200, "days": 30, "hours": 0, "gb": 0}},
        }}

        def db():
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS orders ("
                "id TEXT PRIMARY KEY, tg_user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
                "plan_id TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL, "
                "invoice_id TEXT, invoice_url TEXT, client_uuid TEXT, email TEXT, "
                "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions ("
                "client_uuid TEXT PRIMARY KEY, email TEXT NOT NULL, tg_user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
                "plan_id TEXT NOT NULL, total INTEGER NOT NULL, expiry INTEGER NOT NULL, "
                "status TEXT NOT NULL, suspended_until INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            return conn

        def now_ts():
            return int(time.time())

        def make_order_id(user_id, plan_id):
            raw = f"{{user_id}}:{{plan_id}}:{{time.time()}}:{{uuid.uuid4()}}".encode()
            return hashlib.sha256(raw).hexdigest()[:24]

        def load_creds():
            import yaml
            return yaml.safe_load(CREDS.read_text()) or {{}}

        def tg(method, payload):
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{{TOKEN}}/{{method}}",
                data=data,
                headers={{"Content-Type": "application/json"}},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)

        def send(chat_id, text, keyboard=None):
            payload = {{"chat_id": chat_id, "text": text, "disable_web_page_preview": False}}
            if keyboard:
                payload["reply_markup"] = keyboard
            return tg("sendMessage", payload)

        def answer_callback(callback_id, text=""):
            tg("answerCallbackQuery", {{"callback_query_id": callback_id, "text": text}})

        def menu():
            return {{
                "inline_keyboard": [
                    [
                        {{"text": "Unlim1h", "callback_data": "createh:1:0"}},
                        {{"text": "Unlim2h", "callback_data": "createh:2:0"}},
                        {{"text": "Unlim4h", "callback_data": "createh:4:0"}},
                    ],
                    [
                        {{"text": "Unlim8h", "callback_data": "createh:8:0"}},
                        {{"text": "Unlim20h", "callback_data": "createh:20:0"}},
                    ],
                    [
                        {{"text": "50gb1mo", "callback_data": "create:30:50"}},
                        {{"text": "100gb1mo", "callback_data": "create:30:100"}},
                    ],
                    [
                        {{"text": "200gb1mo", "callback_data": "create:30:200"}},
                        {{"text": "500gb1mo", "callback_data": "create:30:500"}},
                    ],
                    [
                        {{"text": "1tb1mo", "callback_data": "create:30:1024"}},
                        {{"text": "Unlim1mo", "callback_data": "create:30:0"}},
                    ],
                    [
                        {{"text": "Статистика", "callback_data": "stats"}},
                        {{"text": "Онлайн", "callback_data": "users"}},
                    ],
                    [
                        {{"text": "Удалить подписку", "callback_data": "delete_menu"}},
                    ],
                    [
                        {{"text": "Патчи подписок", "callback_data": "patches"}},
                        {{"text": "Процессы бота", "callback_data": "botps"}},
                    ],
                ]
            }}

        def user_menu():
            return {{
                "inline_keyboard": [
                    [{{"text": "Купить VPN", "callback_data": "shop"}}],
                    [{{"text": "Мои подписки", "callback_data": "profile"}}],
                    [{{"text": "Статус серверов", "callback_data": "server_status"}}],
                ]
            }}

        def shop_keyboard():
            rows = []
            for plan_id, plan in PLANS.items():
                rows.append([{{"text": f"{{plan['title']}} - {{plan['price']}} руб.", "callback_data": f"buy:{{plan_id}}"}}])
            rows.append([{{"text": "Назад", "callback_data": "user_menu"}}])
            return {{"inline_keyboard": rows}}

        def plan_text(plan):
            limit = "безлимит" if int(plan.get("gb", 0) or 0) <= 0 else f"{{plan['gb']}} ГБ"
            return f"{{plan['title']}}\\nЛимит: {{limit}}\\nЦена: {{plan['price']}} руб."

        def happ_key_from_subscription(subscription_link):
            if not subscription_link:
                return ""
            return subscription_link

        def server_status_text():
            rows = [f"{{SERVER_ICON}} {{PROFILE_NAME}}"]
            rows.append("VPN: работает")
            rows.append("Протокол: XHTTP")
            rows.append("Пинг в приложении измеряется клиентом автоматически")
            return "\\n".join(rows)

        def user_profile_text(user_id):
            with db() as conn:
                subs = conn.execute(
                    "SELECT * FROM subscriptions WHERE tg_user_id=? ORDER BY created_at DESC",
                    (int(user_id),),
                ).fetchall()
                orders = conn.execute(
                    "SELECT * FROM orders WHERE tg_user_id=? AND status NOT IN ('paid','fulfilled','expired') "
                    "ORDER BY created_at DESC LIMIT 5",
                    (int(user_id),),
                ).fetchall()
            lines = ["Ваш профиль"]
            if subs:
                lines.append("")
                lines.append("Подписки:")
                for sub in subs:
                    plan = PLANS.get(sub["plan_id"], {{"title": sub["plan_id"]}})
                    status = "активна" if sub["status"] == "active" else sub["status"]
                    lines.append(
                        f"{{plan['title']}}: {{status}}\\n"
                        f"  до: {{fmt_expiry(sub['expiry'])}}\\n"
                        f"  лимит: {{'без лимита' if int(sub['total']) <= 0 else fmt_bytes(sub['total'])}}"
                    )
            else:
                lines.append("Активных подписок нет.")
            if orders:
                lines.append("")
                lines.append("Ожидают оплаты:")
                for order in orders:
                    plan = PLANS.get(order["plan_id"], {{"title": order["plan_id"]}})
                    lines.append(f"{{plan['title']}} - {{order['amount']}} руб.: {{order['status']}}")
            return "\\n".join(lines)

        def user_start_text():
            return (
                f"{{BRAND_NAME}}\\n"
                "Выберите тариф, оплатите счет, после оплаты бот автоматически выдаст ссылку на подключение и ключ для HAPP."
            )

        def run_cmd(args):
            try:
                return subprocess.run(args, text=True, capture_output=True, timeout=15)
            except Exception as exc:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(exc))

        def pally_request(path="", payload=None):
            if not PALLY_API_TOKEN:
                raise RuntimeError("Pally API token is not configured. Set MERIDIAN_PALLY_API_TOKEN in /etc/meridian/telegram-bot.env")
            url = PALLY_API_BASE
            if path:
                url = f"{{url}}/{{path.lstrip('/')}}"
            body = dict(payload or {{}})
            if PALLY_SHOP_ID and "shop_id" not in body:
                body["shop_id"] = PALLY_SHOP_ID
            body.setdefault("token", PALLY_API_TOKEN)
            data = json.dumps(body).encode()
            headers = {{
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {{PALLY_API_TOKEN}}",
            }}
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {{"raw": raw}}

        def first_value(data, names, default=""):
            if not isinstance(data, dict):
                return default
            for name in names:
                value = data.get(name)
                if value not in (None, ""):
                    return value
            for value in data.values():
                if isinstance(value, dict):
                    found = first_value(value, names, None)
                    if found not in (None, ""):
                        return found
            return default

        def create_pally_invoice(order_id, plan, user_id):
            payload = {{
                "order_id": order_id,
                "amount": int(plan["price"]),
                "currency": "RUB",
                "description": f"{{BRAND_NAME}}: {{plan['title']}}",
                "comment": f"tg:{{user_id}} plan:{{plan['title']}}",
            }}
            last_error = None
            response = None
            for endpoint in ("invoice/create", "bill/create", "payment/create", "create"):
                try:
                    response = pally_request(endpoint, payload)
                    break
                except Exception as exc:
                    last_error = exc
            if response is None:
                raise RuntimeError(f"Pally invoice create failed: {{last_error}}")
            invoice_id = str(first_value(response, ["invoice_id", "bill_id", "id", "payment_id", "uuid"], order_id))
            invoice_url = str(first_value(response, ["invoice_url", "pay_url", "url", "link", "payment_url"], ""))
            if not invoice_url:
                raise RuntimeError(f"Pally did not return payment URL: {{response}}")
            return invoice_id, invoice_url

        def check_pally_invoice(order):
            invoice_id = order["invoice_id"] or order["id"]
            payload = {{"order_id": order["id"], "invoice_id": invoice_id, "id": invoice_id}}
            last_error = None
            response = None
            for endpoint in ("invoice/status", "bill/status", "payment/status", "status"):
                try:
                    response = pally_request(endpoint, payload)
                    break
                except Exception as exc:
                    last_error = exc
            if response is None:
                raise RuntimeError(f"Pally invoice status failed: {{last_error}}")
            status = str(first_value(response, ["status", "state", "payment_status"], "")).lower()
            if not status and bool(first_value(response, ["paid", "is_paid"], False)):
                status = "paid"
            return status, response

        def create_order(chat_id, user_id, plan_id):
            plan = PLANS.get(plan_id)
            if not plan:
                raise RuntimeError("Unknown plan")
            order_id = make_order_id(user_id, plan_id)
            invoice_id, invoice_url = create_pally_invoice(order_id, plan, user_id)
            ts = now_ts()
            with db() as conn:
                conn.execute(
                    "INSERT INTO orders (id,tg_user_id,chat_id,plan_id,amount,status,invoice_id,invoice_url,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (order_id, int(user_id), int(chat_id), plan_id, int(plan["price"]), "pending", invoice_id, invoice_url, ts, ts),
                )
            return order_id, invoice_url

        def bot_processes_text():
            status = run_cmd(["systemctl", "status", "meridian-telegram-bot", "--no-pager", "--lines", "8"])
            pgrep = run_cmd(["pgrep", "-af", "telegram-bot.py|meridian-telegram-bot"])
            parts = ["meridian-telegram-bot"]
            parts.append("systemctl:")
            parts.append((status.stdout or status.stderr or "нет вывода").strip()[:2500])
            parts.append("")
            parts.append("processes:")
            parts.append((pgrep.stdout or "процессы не найдены").strip()[:1500])
            return "\\n".join(parts)

        def stop_bot_processes():
            cmd = (
                "sleep 2; "
                "systemctl stop meridian-telegram-bot; "
                "pkill -TERM -f telegram-bot.py"
            )
            subprocess.Popen(
                ["sh", "-c", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return "Остановка бота запланирована через 2 секунды. Проверить: /botps"

        def panel_opener(creds):
            panel = creds.get("panel", {{}})
            port = int(panel.get("port") or {DEFAULT_PANEL_PORT})
            path = (panel.get("web_base_path") or "").strip("/")
            base = f"http://127.0.0.1:{{port}}"
            if path:
                base += f"/{{path}}"
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            form = urllib.parse.urlencode({{
                "username": panel.get("username", ""),
                "password": panel.get("password", ""),
            }}).encode()
            opener.open(urllib.request.Request(f"{{base}}/login", data=form, method="POST"), timeout=20)
            return opener, base

        def api(opener, base, path, body=None):
            if body is None:
                with opener.open(f"{{base}}{{path}}", timeout=30) as resp:
                    return json.load(resp)
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                f"{{base}}{{path}}",
                data=data,
                headers={{"Content-Type": "application/json"}},
                method="POST",
            )
            with opener.open(req, timeout=30) as resp:
                return json.load(resp)

        def api_post_empty(opener, base, path):
            req = urllib.request.Request(f"{{base}}{{path}}", method="POST")
            with opener.open(req, timeout=30) as resp:
                return json.load(resp)

        def list_inbounds(opener, base):
            data = api(opener, base, "/panel/api/inbounds/list")
            if not data.get("success"):
                raise RuntimeError(data.get("msg") or "cannot list inbounds")
            return data.get("obj") or []

        def xhttp_inbound(inbounds):
            for inbound in inbounds:
                if inbound.get("remark") == "VLESS-Reality-XHTTP":
                    return inbound
            raise RuntimeError("XHTTP inbound is not configured")

        def split_relays(creds):
            return [
                relay for relay in creds.get("relays", [])
                if relay.get("mode") == "split"
                and relay.get("panel_url")
                and relay.get("panel_username")
                and relay.get("panel_password")
                and relay.get("xhttp_path")
            ]

        def split_public_relay(creds):
            relays = split_relays(creds)
            return relays[0] if relays else None

        def xhttp_clients(inbound):
            try:
                settings = json.loads(inbound.get("settings") or "{{}}")
            except Exception:
                settings = {{}}
            return settings.get("clients") or []

        def inbound_clients(inbound):
            try:
                settings = json.loads(inbound.get("settings") or "{{}}")
            except Exception:
                settings = {{}}
            return settings.get("clients") or []

        def build_xhttp_url(creds, client_uuid):
            server = creds.get("server", {{}})
            protocols = creds.get("protocols", {{}})
            xhttp = protocols.get("xhttp", {{}})
            relay = split_public_relay(creds)
            host = (
                relay.get("public_host") or relay.get("ip") or ""
                if relay else
                server.get("domain") or server.get("ip") or ""
            )
            path = (relay.get("xhttp_path") if relay else xhttp.get("xhttp_path")) or ""
            if not host or not path:
                raise RuntimeError("XHTTP URL cannot be built: host/path missing")
            return (
                f"vless://{{client_uuid}}@{{host}}:443"
                f"?encryption=none&security=tls&sni={{host}}&fp=chrome"
                f"&type=xhttp&path=%2F{{path}}#{{urllib.parse.quote(server_display_name())}}"
            )

        def subscription_url(creds, client_uuid):
            server = creds.get("server", {{}})
            panel = creds.get("panel", {{}})
            host = server.get("domain") or server.get("ip") or ""
            info_path = panel.get("info_page_path") or ""
            if not host or not info_path:
                return ""
            return f"https://{{host}}/{{info_path}}/{{client_uuid}}/sub.txt"

        def server_display_name():
            title = PROFILE_NAME or BRAND_NAME
            if SERVER_ICON and not title.startswith(SERVER_ICON):
                title = f"{{SERVER_ICON}} {{title}}"
            return title

        def profile_title():
            return BRAND_NAME or "PridVPN"

        def subscription_payload(urls, total=0, expiry=0):
            import base64
            title_b64 = base64.b64encode(profile_title().encode()).decode()
            lines = [
                f"#profile-title: base64:{{title_b64}}",
                "#profile-update-interval: 12",
                f"#subscription-userinfo: upload=0; download=0; total={{int(total or 0)}}; expire={{int((expiry or 0) / 1000)}}",
            ]
            lines.extend(url for url in urls if url)
            return base64.b64encode("\\n".join(lines).encode()).decode()

        def publish_subscription(client_uuid, urls, total=0, expiry=0):
            target = WEB_ROOT / client_uuid
            target.mkdir(parents=True, exist_ok=True)
            (target / "sub.txt").write_text(subscription_payload(urls, total=total, expiry=expiry))
            os.chmod(target / "sub.txt", 0o644)

        def remote_panel_opener(relay):
            base = relay.get("panel_url", "").rstrip("/")
            if not base:
                raise RuntimeError("split relay panel_url is empty")
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            form = urllib.parse.urlencode({{
                "username": relay.get("panel_username", ""),
                "password": relay.get("panel_password", ""),
            }}).encode()
            opener.open(urllib.request.Request(f"{{base}}/login", data=form, method="POST"), timeout=25)
            return opener, base

        def sync_split_add(creds, client_uuid, email, total, expiry, enabled=True):
            settings = {{
                "clients": [{{
                    "id": client_uuid,
                    "flow": "",
                    "email": email,
                    "limitIp": 0,
                    "totalGB": int(total or 0),
                    "expiryTime": int(expiry or 0),
                    "enable": bool(enabled),
                    "tgId": "",
                    "subId": "",
                    "reset": 0,
                }}]
            }}
            for relay in split_relays(creds):
                try:
                    opener, base = remote_panel_opener(relay)
                    inbound = xhttp_inbound(list_inbounds(opener, base))
                    existing = [c for c in xhttp_clients(inbound) if c.get("id") == client_uuid]
                    if existing:
                        continue
                    data = api(opener, base, "/panel/api/inbounds/addClient", {{"id": inbound["id"], "settings": json.dumps(settings)}})
                    if not data.get("success"):
                        raise RuntimeError(data.get("msg") or "split relay add client failed")
                except Exception:
                    traceback.print_exc()

        def sync_split_delete(creds, client_uuid):
            for relay in split_relays(creds):
                try:
                    opener, base = remote_panel_opener(relay)
                    for inbound in list_inbounds(opener, base):
                        for client in inbound_clients(inbound):
                            if client.get("id") != client_uuid:
                                continue
                            api_post_empty(opener, base, f"/panel/api/inbounds/{{inbound['id']}}/delClient/{{client_uuid}}")
                except Exception:
                    traceback.print_exc()

        def sync_split_enabled(creds, client_uuid, enabled):
            for relay in split_relays(creds):
                try:
                    opener, base = remote_panel_opener(relay)
                    for inbound in list_inbounds(opener, base):
                        for client in inbound_clients(inbound):
                            if client.get("id") != client_uuid:
                                continue
                            updated = dict(client)
                            updated["enable"] = bool(enabled)
                            api(opener, base, f"/panel/api/inbounds/updateClient/{{client_uuid}}", {{
                                "id": inbound["id"],
                                "settings": json.dumps({{"clients": [updated]}}),
                            }})
                except Exception:
                    traceback.print_exc()

        def create_subscription(days, traffic_gb, hours=0, plan_name="", tg_user_id=0, chat_id=0):
            creds = load_creds()
            opener, base = panel_opener(creds)
            xhttp_ib = xhttp_inbound(list_inbounds(opener, base))
            client_uuid = str(uuid.uuid4())
            suffix = int(time.time())
            plan_slug = "".join(ch.lower() for ch in plan_name if ch.isalnum())[:24]
            email = f"xhttp-{{int(tg_user_id or 0) or 'tg'}}-{{plan_slug or 'vpn'}}-{{suffix}}"
            ttl_seconds = hours * 3600 if hours > 0 else days * 86400
            expiry = int((time.time() + ttl_seconds) * 1000) if ttl_seconds > 0 else 0
            total = int(traffic_gb * 1024 * 1024 * 1024) if traffic_gb > 0 else 0
            xhttp_settings = {{
                "clients": [{{
                    "id": client_uuid,
                    "flow": "",
                    "email": email,
                    "limitIp": 0,
                    "totalGB": total,
                    "expiryTime": expiry,
                    "enable": True,
                    "tgId": "",
                    "subId": "",
                    "reset": 0,
                }}]
            }}
            data = api(opener, base, "/panel/api/inbounds/addClient", {{"id": xhttp_ib["id"], "settings": json.dumps(xhttp_settings)}})
            if not data.get("success"):
                raise RuntimeError(data.get("msg") or "add client failed")
            sync_split_add(creds, client_uuid, email, total, expiry, enabled=True)
            key = build_xhttp_url(creds, client_uuid)
            publish_subscription(client_uuid, [key], total=total, expiry=expiry)
            return subscription_url(creds, client_uuid), key, client_uuid, email, total, expiry

        def record_subscription(client_uuid, email, tg_user_id, chat_id, plan_id, total, expiry):
            with db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO subscriptions "
                    "(client_uuid,email,tg_user_id,chat_id,plan_id,total,expiry,status,suspended_until,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        client_uuid,
                        email,
                        int(tg_user_id or 0),
                        int(chat_id or 0),
                        plan_id,
                        int(total or 0),
                        int(expiry or 0),
                        "active",
                        0,
                        now_ts(),
                    ),
                )

        def fulfill_order(order):
            plan = PLANS.get(order["plan_id"])
            if not plan:
                raise RuntimeError(f"Unknown plan {{order['plan_id']}}")
            link, key, client_uuid, email, total, expiry = create_subscription(
                int(plan.get("days", 0) or 0),
                int(plan.get("gb", 0) or 0),
                hours=int(plan.get("hours", 0) or 0),
                plan_name=plan["title"],
                tg_user_id=order["tg_user_id"],
                chat_id=order["chat_id"],
            )
            record_subscription(client_uuid, email, order["tg_user_id"], order["chat_id"], order["plan_id"], total, expiry)
            with db() as conn:
                conn.execute(
                    "UPDATE orders SET status='fulfilled', client_uuid=?, email=?, updated_at=? WHERE id=?",
                    (client_uuid, email, now_ts(), order["id"]),
                )
            send(
                order["chat_id"],
                f"Оплата получена. Подписка активирована: {{plan['title']}}\\n"
                f"{{plan_summary(total, expiry)}}\\n\\n"
                f"Страница подключения:\\n{{link}}\\n\\n"
                f"Ключ для HAPP:\\n{{key}}",
            )
            return client_uuid

        def fmt_bytes(value):
            value = int(value or 0)
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if value < 1024 or unit == "TB":
                    return f"{{value:.1f}} {{unit}}" if unit != "B" else f"{{value}} B"
                value /= 1024

        def fmt_expiry(expiry_ms):
            expiry_ms = int(expiry_ms or 0)
            if expiry_ms <= 0:
                return "бессрочно"
            return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expiry_ms / 1000))

        def plan_summary(total, expiry):
            limit_text = "без лимита" if int(total or 0) <= 0 else fmt_bytes(total)
            return f"Лимит: {{limit_text}}\\nДействует до: {{fmt_expiry(expiry)}}"

        def fmt_mtime(path):
            try:
                return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(path.stat().st_mtime))
            except FileNotFoundError:
                return "файл отсутствует"

        def read_subscription_payload(client_uuid):
            import base64
            path = WEB_ROOT / client_uuid / "sub.txt"
            if not path.exists():
                return path, ""
            raw = path.read_text().strip()
            try:
                return path, base64.b64decode(raw + "===" ).decode(errors="replace")
            except Exception:
                return path, ""

        def patches_text():
            creds = load_creds()
            opener, base = panel_opener(creds)
            rows = []
            for _, client in subscription_clients(opener, base):
                email = client.get("email", "")
                client_uuid = client.get("id", "")
                total = int(client.get("totalGB", 0) or 0)
                expiry = int(client.get("expiryTime", 0) or 0)
                path, decoded = read_subscription_payload(client_uuid)
                has_title = "#profile-title:" in decoded
                has_info = "#subscription-userinfo:" in decoded
                status = "patched" if has_title and has_info else "old"
                rows.append(
                    f"{{email}}\\n"
                    f"  uuid: {{client_uuid}}\\n"
                    f"  файл: {{fmt_mtime(path)}}\\n"
                    f"  статус: {{status}}\\n"
                    f"  лимит: {{'без лимита' if total <= 0 else fmt_bytes(total)}}\\n"
                    f"  до: {{fmt_expiry(expiry)}}\\n"
                    f"  url: {{subscription_url(creds, client_uuid)}}"
                )
            return "\\n\\n".join(rows[:20]) if rows else "Подписок пока нет."

        def client_traffic(opener, base, email):
            quoted = urllib.parse.quote(email, safe="")
            data = api(opener, base, f"/panel/api/inbounds/getClientTraffics/{{quoted}}")
            return data.get("obj") if data.get("success") else {{}}

        def subscription_clients(opener, base):
            inbound = xhttp_inbound(list_inbounds(opener, base))
            clients = []
            for client in xhttp_clients(inbound):
                email = client.get("email", "")
                client_uuid = client.get("id", "")
                if email.startswith("xhttp-") and client_uuid:
                    clients.append((inbound, client))
            return clients

        def delete_menu_keyboard():
            creds = load_creds()
            opener, base = panel_opener(creds)
            rows = []
            for _, client in subscription_clients(opener, base)[:30]:
                email = client.get("email", "")
                client_uuid = client.get("id", "")
                label = email.replace("xhttp-", "", 1)[:32] or client_uuid[:8]
                rows.append([{{"text": f"Удалить {{label}}", "callback_data": f"delete:{{client_uuid}}"}}])
            rows.append([{{"text": "Назад", "callback_data": "menu"}}])
            return {{"inline_keyboard": rows}}

        def delete_subscription(client_uuid):
            creds = load_creds()
            opener, base = panel_opener(creds)
            deleted_email = ""
            for inbound in list_inbounds(opener, base):
                found = None
                for client in inbound_clients(inbound):
                    if client.get("id") == client_uuid:
                        found = client
                        break
                if not found:
                    continue
                data = api_post_empty(opener, base, f"/panel/api/inbounds/{{inbound['id']}}/delClient/{{client_uuid}}")
                if not data.get("success"):
                    raise RuntimeError(data.get("msg") or f"delete client failed in {{inbound.get('remark', inbound.get('id'))}}")
                deleted_email = deleted_email or found.get("email", client_uuid)
            if deleted_email:
                sync_split_delete(creds, client_uuid)
                target = WEB_ROOT / client_uuid
                if target.exists():
                    for child in target.iterdir():
                        child.unlink()
                    target.rmdir()
                with db() as conn:
                    conn.execute("UPDATE subscriptions SET status='deleted' WHERE client_uuid=?", (client_uuid,))
                return deleted_email
            raise RuntimeError("subscription not found")

        def update_client_enabled(client_uuid, enabled):
            creds = load_creds()
            opener, base = panel_opener(creds)
            for inbound in list_inbounds(opener, base):
                for client in inbound_clients(inbound):
                    if client.get("id") != client_uuid:
                        continue
                    updated = dict(client)
                    updated["enable"] = bool(enabled)
                    settings = {{"clients": [updated]}}
                    data = api(opener, base, f"/panel/api/inbounds/updateClient/{{client_uuid}}", {{
                        "id": inbound["id"],
                        "settings": json.dumps(settings),
                    }})
                    if not data.get("success"):
                        raise RuntimeError(data.get("msg") or "update client failed")
                    sync_split_enabled(creds, client_uuid, enabled)
                    return updated.get("email", client_uuid)
            raise RuntimeError("subscription not found")

        def expire_old_subscriptions():
            now_ms = int(time.time() * 1000)
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM subscriptions WHERE status IN ('active','suspended') AND expiry > 0 AND expiry <= ?",
                    (now_ms,),
                ).fetchall()
            for row in rows:
                try:
                    delete_subscription(row["client_uuid"])
                    send(row["chat_id"], "Ваша подписка закончилась и была удалена.")
                except Exception:
                    traceback.print_exc()

        def restore_suspended_subscriptions():
            now = now_ts()
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM subscriptions WHERE status='suspended' AND suspended_until > 0 AND suspended_until <= ?",
                    (now,),
                ).fetchall()
            for row in rows:
                try:
                    update_client_enabled(row["client_uuid"], True)
                    with db() as conn:
                        conn.execute(
                            "UPDATE subscriptions SET status='active', suspended_until=0 WHERE client_uuid=?",
                            (row["client_uuid"],),
                        )
                    send(row["chat_id"], "VPN-ключ снова включен.")
                except Exception:
                    traceback.print_exc()

        def check_pending_payments():
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE status IN ('pending','created','paid','success','completed','confirmed','done') "
                    "AND client_uuid IS NULL ORDER BY created_at ASC LIMIT 20"
                ).fetchall()
            for order in rows:
                try:
                    if now_ts() - int(order["created_at"]) > 86400:
                        with db() as conn:
                            conn.execute("UPDATE orders SET status='expired', updated_at=? WHERE id=?", (now_ts(), order["id"]))
                        continue
                    if order["status"] in PALLY_SUCCESS_STATUSES:
                        fulfill_order(order)
                        continue
                    status, _response = check_pally_invoice(order)
                    if status:
                        with db() as conn:
                            conn.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, now_ts(), order["id"]))
                    if status in PALLY_SUCCESS_STATUSES:
                        fulfill_order(order)
                except Exception:
                    traceback.print_exc()

        def get_state(key, default=""):
            with db() as conn:
                row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

        def set_state(key, value):
            with db() as conn:
                conn.execute("INSERT OR REPLACE INTO state (key,value) VALUES (?,?)", (key, str(value)))

        def active_torrent_log():
            for path in TORRENT_LOG_PATHS:
                if path.exists():
                    return path
            return None

        def suspend_for_torrent(email):
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE email=? AND status='active'",
                    (email,),
                ).fetchone()
            if not row:
                return
            send(row["chat_id"], "Обнаружена попытка использовать торрент через VPN. Ключ будет отключен на 10 минут.")
            time.sleep(5)
            update_client_enabled(row["client_uuid"], False)
            until = now_ts() + 10 * 60
            with db() as conn:
                conn.execute(
                    "UPDATE subscriptions SET status='suspended', suspended_until=? WHERE client_uuid=?",
                    (until, row["client_uuid"]),
                )

        def monitor_torrent_usage():
            path = active_torrent_log()
            if not path:
                return
            key = f"torrent_log_pos:{{path}}"
            try:
                pos = int(get_state(key, "0") or "0")
                size = path.stat().st_size
                if pos > size:
                    pos = 0
                with path.open("r", errors="ignore") as fh:
                    fh.seek(pos)
                    data = fh.read(200000)
                    set_state(key, fh.tell())
                if "bittorrent" not in data.lower() and "torrent" not in data.lower():
                    return
                emails = set(re.findall(r"xhttp-[A-Za-z0-9_.@-]+", data))
                for email in emails:
                    if re.search(re.escape(email) + r".*(bittorrent|torrent)", data, re.IGNORECASE) or re.search(
                        r"(bittorrent|torrent).*" + re.escape(email), data, re.IGNORECASE
                    ):
                        suspend_for_torrent(email)
            except Exception:
                traceback.print_exc()

        def stats_text(online_only=False):
            creds = load_creds()
            opener, base = panel_opener(creds)
            now_ms = int(time.time() * 1000)
            rows = []
            online = 0
            for _, client in subscription_clients(opener, base):
                email = client.get("email", "")
                tr = client_traffic(opener, base, email)
                total = int(tr.get("up", 0)) + int(tr.get("down", 0))
                last = int(tr.get("lastOnline", 0) or 0)
                is_online = last and now_ms - last < 5 * 60 * 1000
                if is_online:
                    online += 1
                if online_only and not is_online:
                    continue
                limit = int(client.get("totalGB", 0) or 0)
                expiry = int(client.get("expiryTime", 0) or 0)
                limit_text = "без лимита" if limit <= 0 else fmt_bytes(limit)
                rows.append(
                    f"{{email}}: {{fmt_bytes(total)}} / {{limit_text}}, до {{fmt_expiry(expiry)}}"
                    f"{{' онлайн' if is_online else ''}}"
                )
            if online_only:
                return f"Сейчас с VPN: {{online}}\\n" + ("\\n".join(rows) if rows else "Активных подключений нет.")
            return f"Подписок: {{len(rows)}}\\nСейчас с VPN: {{online}}\\n" + ("\\n".join(rows[:25]) if rows else "Подписок пока нет.")

        def handle_message(msg):
            chat_id = msg.get("chat", {{}}).get("id")
            user_id = msg.get("from", {{}}).get("id")
            text = (msg.get("text") or "").strip()
            if user_id != ADMIN_ID:
                if text.startswith("/profile"):
                    send(chat_id, user_profile_text(user_id), user_menu())
                elif text.startswith("/status"):
                    send(chat_id, server_status_text(), user_menu())
                else:
                    send(chat_id, user_start_text(), user_menu())
                return
            if text.startswith("/create"):
                parts = text.split()
                days = int(parts[1]) if len(parts) > 1 else 30
                gb = int(parts[2]) if len(parts) > 2 else 0
                send(chat_id, "Создается подписка...")
                link, key, client_uuid, email, total, expiry = create_subscription(
                    days, gb, plan_name=f"custom{{days}}d{{gb}}gb", tg_user_id=user_id, chat_id=chat_id
                )
                record_subscription(client_uuid, email, user_id, chat_id, "admin", total, expiry)
                send(chat_id, f"Подписка создана: {{email}}\\n{{plan_summary(total, expiry)}}\\n{{link}}\\n\\nHAPP:\\n{{key}}")
            elif text.startswith("/stats"):
                send(chat_id, stats_text(False))
            elif text.startswith("/users"):
                send(chat_id, stats_text(True))
            elif text.startswith("/delete"):
                send(chat_id, "Выберите подписку для удаления:", delete_menu_keyboard())
            elif text.startswith("/patches"):
                send(chat_id, patches_text())
            elif text.startswith("/botps"):
                send(chat_id, bot_processes_text())
            elif text.startswith("/botstop"):
                send(chat_id, stop_bot_processes())
            else:
                send(chat_id, "Панель управления PridVPN", menu())

        def handle_callback(cb):
            user_id = cb.get("from", {{}}).get("id")
            chat_id = cb.get("message", {{}}).get("chat", {{}}).get("id")
            data = cb.get("data") or ""
            answer_callback(cb.get("id"), "Выполняю")
            if user_id != ADMIN_ID:
                if data == "user_menu":
                    send(chat_id, user_start_text(), user_menu())
                elif data == "shop":
                    send(chat_id, "Выберите тариф:", shop_keyboard())
                elif data == "profile":
                    send(chat_id, user_profile_text(user_id), user_menu())
                elif data == "server_status":
                    send(chat_id, server_status_text(), user_menu())
                elif data.startswith("buy:"):
                    _, plan_id = data.split(":", 1)
                    plan = PLANS.get(plan_id)
                    if not plan:
                        send(chat_id, "Тариф не найден.", user_menu())
                        return
                    try:
                        order_id, invoice_url = create_order(chat_id, user_id, plan_id)
                        send(
                            chat_id,
                            f"Счет создан.\\n{{plan_text(plan)}}\\n\\n"
                            f"Оплатите по ссылке:\\n{{invoice_url}}\\n\\n"
                            "После оплаты бот автоматически пришлет подписку.",
                            user_menu(),
                        )
                    except Exception as exc:
                        send(chat_id, f"Не удалось создать счет: {{exc}}", user_menu())
                else:
                    send(chat_id, user_start_text(), user_menu())
                return
            if data.startswith("create:"):
                _, days, gb = data.split(":")
                plan = {{
                    "create:30:50": "50gb1mo",
                    "create:30:100": "100gb1mo",
                    "create:30:200": "200gb1mo",
                    "create:30:500": "500gb1mo",
                    "create:30:1024": "1tb1mo",
                    "create:30:0": "Unlim1mo",
                }}.get(data, f"custom{{days}}d{{gb}}gb")
                send(chat_id, "Создается подписка...")
                link, key, client_uuid, email, total, expiry = create_subscription(
                    int(days), int(gb), plan_name=plan, tg_user_id=user_id, chat_id=chat_id
                )
                record_subscription(client_uuid, email, user_id, chat_id, "admin", total, expiry)
                send(chat_id, f"Подписка {{plan}} создана: {{email}}\\n{{plan_summary(total, expiry)}}\\n{{link}}\\n\\nHAPP:\\n{{key}}")
            elif data.startswith("createh:"):
                _, hours, gb = data.split(":")
                plan = {{
                    "createh:1:0": "Unlim1h",
                    "createh:2:0": "Unlim2h",
                    "createh:4:0": "Unlim4h",
                    "createh:8:0": "Unlim8h",
                    "createh:20:0": "Unlim20h",
                }}.get(data, f"custom{{hours}}h{{gb}}gb")
                send(chat_id, "Создается подписка...")
                link, key, client_uuid, email, total, expiry = create_subscription(
                    0, int(gb), hours=int(hours), plan_name=plan, tg_user_id=user_id, chat_id=chat_id
                )
                record_subscription(client_uuid, email, user_id, chat_id, "admin", total, expiry)
                send(chat_id, f"Подписка {{plan}} создана: {{email}}\\n{{plan_summary(total, expiry)}}\\n{{link}}\\n\\nHAPP:\\n{{key}}")
            elif data == "stats":
                send(chat_id, stats_text(False))
            elif data == "users":
                send(chat_id, stats_text(True))
            elif data == "delete_menu":
                send(chat_id, "Выберите подписку для удаления:", delete_menu_keyboard())
            elif data == "patches":
                send(chat_id, patches_text())
            elif data == "botps":
                send(chat_id, bot_processes_text())
            elif data.startswith("delete:"):
                _, client_uuid = data.split(":", 1)
                email = delete_subscription(client_uuid)
                send(chat_id, f"Подписка удалена: {{email}}", menu())
            elif data == "menu":
                send(chat_id, "Панель управления PridVPN", menu())

        def main():
            if not TOKEN:
                raise RuntimeError("MERIDIAN_TELEGRAM_BOT_TOKEN is empty")
            db().close()
            offset = 0
            last_jobs = 0
            while True:
                try:
                    if time.time() - last_jobs >= 30:
                        check_pending_payments()
                        expire_old_subscriptions()
                        restore_suspended_subscriptions()
                        monitor_torrent_usage()
                        last_jobs = time.time()
                    url = f"https://api.telegram.org/bot{{TOKEN}}/getUpdates?timeout=25&offset={{offset}}"
                    with urllib.request.urlopen(url, timeout=35) as resp:
                        data = json.load(resp)
                    for upd in data.get("result", []):
                        offset = max(offset, int(upd["update_id"]) + 1)
                        if "message" in upd:
                            handle_message(upd["message"])
                        elif "callback_query" in upd:
                            handle_callback(upd["callback_query"])
                except Exception:
                    traceback.print_exc()
                    time.sleep(5)

        if __name__ == "__main__":
            main()
    """)


class DeployTelegramBot:
    """Deploy and start the PridVPN Telegram management bot."""

    name = "Deploy Telegram bot"

    def __init__(self, token: str = TELEGRAM_BOT_TOKEN, admin_id: int = TELEGRAM_ADMIN_ID) -> None:
        self.token = token
        self.admin_id = admin_id

    def run(self, conn: ServerConnection, ctx: ProvisionContext) -> StepResult:
        if not self.token:
            return StepResult(name=self.name, status="skipped", detail="Telegram bot token is empty")

        script = _render_telegram_bot_script()
        q_script = shlex.quote(script)
        conn.run("mkdir -p /etc/meridian", timeout=15)
        result = conn.run(f"printf '%s' {q_script} > /etc/meridian/telegram-bot.py", timeout=30)
        if result.returncode != 0:
            return StepResult(name=self.name, status="failed", detail=f"Failed to write bot script: {result.stderr}")
        conn.run("chmod 700 /etc/meridian/telegram-bot.py", timeout=15)
        result = conn.run("python3 -m py_compile /etc/meridian/telegram-bot.py", timeout=15)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return StepResult(name=self.name, status="failed", detail=f"Telegram bot script has syntax errors: {detail}")

        env_stub = textwrap.dedent("""\
            # Pally merchant API settings.
            # Fill these values after deploy, then restart:
            #   sudo systemctl restart meridian-telegram-bot
            MERIDIAN_PALLY_API_TOKEN=
            MERIDIAN_PALLY_SHOP_ID=
            MERIDIAN_PALLY_API_BASE=https://pally.info/merchant/api
        """)
        q_env_stub = shlex.quote(env_stub)
        result = conn.run(
            f"test -f /etc/meridian/telegram-bot.env || printf '%s' {q_env_stub} > /etc/meridian/telegram-bot.env",
            timeout=15,
        )
        if result.returncode != 0:
            return StepResult(name=self.name, status="failed", detail=f"Failed to write bot env stub: {result.stderr}")
        conn.run("chmod 600 /etc/meridian/telegram-bot.env", timeout=15)

        escaped_token = self.token.replace("\\", "\\\\").replace('"', '\\"')
        service = textwrap.dedent(f"""\
            [Unit]
            Description=PridVPN Telegram bot
            After=network-online.target 3x-ui.service nginx.service
            Wants=network-online.target

            [Service]
            Type=simple
            Environment="MERIDIAN_TELEGRAM_BOT_TOKEN={escaped_token}"
            Environment="MERIDIAN_TELEGRAM_ADMIN_ID={self.admin_id}"
            EnvironmentFile=-/etc/meridian/telegram-bot.env
            ExecStart=/usr/bin/python3 /etc/meridian/telegram-bot.py
            Restart=always
            RestartSec=5
            User=root

            [Install]
            WantedBy=multi-user.target
        """)
        q_service = shlex.quote(service)
        result = conn.run(f"printf '%s' {q_service} > /etc/systemd/system/meridian-telegram-bot.service", timeout=15)
        if result.returncode != 0:
            return StepResult(name=self.name, status="failed", detail=f"Failed to write service: {result.stderr}")

        result = conn.run(
            "systemctl daemon-reload && systemctl enable meridian-telegram-bot && systemctl restart meridian-telegram-bot",
            timeout=30,
        )
        if result.returncode != 0:
            return StepResult(name=self.name, status="failed", detail=f"Failed to start bot: {result.stderr}")

        result = conn.run("systemctl is-active --quiet meridian-telegram-bot", timeout=10)
        if result.returncode != 0:
            logs = conn.run("journalctl -u meridian-telegram-bot -n 30 --no-pager", timeout=15)
            detail = (logs.stdout or logs.stderr).strip()
            return StepResult(name=self.name, status="failed", detail=f"Telegram bot is not active:\n{detail}")

        return StepResult(name=self.name, status="changed", detail="Telegram bot is running")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_domain_dns(conn: ServerConnection, domain: str, server_ip: str) -> str | None:
    """Check if the domain resolves to the server IP.

    Returns an error message if DNS check fails, None if OK.
    """
    q_domain = shlex.quote(domain)
    result = conn.run(f"dig +short {q_domain}", timeout=15)
    resolved = result.stdout.strip() if result.returncode == 0 else ""

    if not resolved:
        # Empty DNS response -- might be a new domain, let it pass
        return None

    if resolved != server_ip:
        return (
            f"{domain} does not resolve to this server's IP ({server_ip}).\n"
            f"DNS returned: {resolved}\n\n"
            f"The domain must point DIRECTLY to this server for TLS certificates.\n\n"
            f"Fix: In Cloudflare, set the A record to 'DNS only' (grey cloud), then re-run.\n"
            f"After setup succeeds, switch to 'Proxied' (orange cloud).\n\n"
            f"To skip this check: use skip_dns_check=True"
        )

    return None
