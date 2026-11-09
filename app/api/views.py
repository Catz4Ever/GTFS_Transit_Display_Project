import html
import os

from app.api.pool import get_conn
from transit.services.city_admin_service import get_city_admin_overview


def _credentials_warning_banner() -> str:
    if os.getenv("CREDENTIALS_KEY", "").strip():
        return ""
    return (
        '<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:4px;'
        'padding:10px 16px;margin-bottom:16px;font-size:13px">'
        '<strong>⚠ Credential encryption not configured.</strong> '
        'API keys will be stored encrypted automatically on first save, but you should set a persistent '
        '<code>CREDENTIALS_KEY</code> in your <code>.env</code> file to ensure keys survive container restarts. '
        'Generate one with: <code>python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"</code>'
        '</div>'
    )


def load_city_rows():
    with get_conn() as conn:
        return get_city_admin_overview(conn)


def build_city_groups():
    rows = []
    try:
        rows = load_city_rows()
    except Exception:
        rows = []

    grouped = {}
    for row in rows:
        (
            city_code,
            city_name,
            timezone_name,
            rollover_hour,
            is_enabled,
            sim_enabled,
            sim_force_date,
            sim_force_time,
            feed_type,
            source_kind,
            source_value,
            source_enabled,
            refresh_interval_seconds,
            *extra,
        ) = row
        source_encrypted = bool(extra[0]) if extra else False
        grouped.setdefault(
            city_code,
            {
                "city_name": city_name,
                "timezone_name": timezone_name,
                "rollover_hour": rollover_hour,
                "is_enabled": is_enabled,
                "sim_enabled": sim_enabled,
                "sim_force_date": sim_force_date,
                "sim_force_time": sim_force_time,
                "sources": [],
            },
        )
        if feed_type:
            grouped[city_code]["sources"].append(
                {
                    "feed_type": feed_type,
                    "source_kind": source_kind,
                    "source_value": source_value,
                    "is_enabled": source_enabled,
                    "refresh_interval_seconds": refresh_interval_seconds,
                    "source_encrypted": source_encrypted,
                }
            )
    return grouped


def render_layout(title: str, body: str) -> str:
    return f'''
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>{html.escape(title)}</title>
      <style>
        body {{
          font-family: Arial, sans-serif;
          max-width: 1150px;
          margin: 0 auto;
          padding: 24px;
          line-height: 1.4;
          background: #f7f7f9;
          color: #222;
        }}
        h1, h2, h3 {{
          margin-bottom: 12px;
        }}
        .topnav {{
          display: flex;
          gap: 16px;
          margin-bottom: 20px;
          flex-wrap: wrap;
        }}
        .topnav a {{
          text-decoration: none;
          color: #2563eb;
          font-weight: bold;
        }}
        .panel {{
          background: white;
          border: 1px solid #ddd;
          border-radius: 12px;
          padding: 20px;
          margin-bottom: 20px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }}
        .grid {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
          gap: 16px;
        }}
        .card, .city-card {{
          background: white;
          border: 1px solid #ddd;
          border-radius: 12px;
          padding: 16px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }}
        .card a, .city-card a {{
          color: #2563eb;
          text-decoration: none;
          font-weight: bold;
        }}
        label {{
          display: block;
          font-weight: bold;
          margin-top: 12px;
          margin-bottom: 4px;
        }}
        input[type="text"], input[type="number"], input[type="file"], select {{
          width: 100%;
          max-width: 700px;
          padding: 10px;
          border: 1px solid #ccc;
          border-radius: 8px;
          box-sizing: border-box;
        }}
        input[type="checkbox"] {{
          margin-right: 8px;
        }}
        button {{
          margin-top: 18px;
          padding: 12px 18px;
          border: none;
          border-radius: 10px;
          cursor: pointer;
          background: #2563eb;
          color: white;
          font-weight: bold;
        }}
        button:hover {{
          background: #1d4ed8;
        }}
        .message {{
          padding: 12px 14px;
          border-radius: 8px;
          margin-bottom: 16px;
        }}
        .message.success {{
          background: #e8f7ec;
          color: #166534;
          border: 1px solid #bbf7d0;
        }}
        .message.error {{
          background: #fef2f2;
          color: #991b1b;
          border: 1px solid #fecaca;
        }}
        table {{
          width: 100%;
          border-collapse: collapse;
        }}
        th, td {{
          padding: 10px;
          border-bottom: 1px solid #e5e7eb;
          text-align: left;
          vertical-align: top;
        }}
        .muted {{
          color: #666;
          font-size: 0.95em;
        }}
        .badge {{
          display: inline-block;
          padding: 2px 8px;
          border-radius: 999px;
          font-size: 12px;
          background: #eef2ff;
          color: #3730a3;
          margin-right: 6px;
        }}
        .badge.enabled {{
          background: #dcfce7;
          color: #166534;
        }}
        .badge.disabled {{
          background: #fee2e2;
          color: #991b1b;
        }}
        .inline-form {{
          display: inline;
        }}
        .secondary-button {{
          margin-top: 6px;
          padding: 7px 10px;
          border-radius: 8px;
          background: #374151;
          color: white;
          font-size: 12px;
        }}
        .secondary-button:hover {{
          background: #111827;
        }}
        .import-log-panel {{
          margin-top: 20px;
          display: none;
        }}
        .import-log-box {{
          min-height: 240px;
          max-height: 460px;
          overflow: auto;
          padding: 14px;
          border-radius: 10px;
          background: #111827;
          color: #d1fae5;
          border: 1px solid #374151;
          white-space: pre-wrap;
          font-family: Consolas, Monaco, "Courier New", monospace;
          font-size: 13px;
        }}
      </style>
    </head>
    <body>
      <div class="topnav">
        <a href="/admin">Admin Home</a>
        <a href="/admin/cities">Cities</a>
        <a href="/admin/cities/new">Import City</a>
        <a href="/health">Health</a>
        <a href="/admin/logout" style="margin-left:auto;color:#e57">Logout</a>
      </div>
      {_credentials_warning_banner()}
      {body}
    </body>
    </html>
    '''


def render_admin_home() -> str:
    grouped = build_city_groups()
    city_count = len(grouped)
    enabled_count = sum(1 for _, d in grouped.items() if d["is_enabled"])
    sim_enabled_count = sum(1 for _, d in grouped.items() if d["sim_enabled"])
    feed_count = sum(len(d["sources"]) for _, d in grouped.items())

    body = f'''
    <h1>Transit Admin</h1>
    <div class="panel">
      <p class="muted">Central place for onboarding cities and managing runtime behavior.</p>
      <div class="grid">
        <div class="card">
          <h3>Cities</h3>
          <div><strong>{city_count}</strong> total</div>
          <div><strong>{enabled_count}</strong> enabled</div>
          <div><strong>{sim_enabled_count}</strong> with simulation enabled</div>
          <div style="margin-top: 12px;"><a href="/admin/cities">Manage cities</a></div>
        </div>
        <div class="card">
          <h3>Import</h3>
          <div>Upload a GTFS zip/rar or provide a download URL, and configure GTFS-RT sources.</div>
          <div style="margin-top: 12px;"><a href="/admin/cities/new">Import a new city</a></div>
        </div>
        <div class="card">
          <h3>Feeds</h3>
          <div><strong>{feed_count}</strong> GTFS-RT sources configured</div>
          <div style="margin-top: 12px;"><a href="/admin/cities">Review feed settings</a></div>
        </div>
      </div>
    </div>
    '''
    return render_layout("Transit Admin", body)


def render_city_list_page(message: str = "", is_error: bool = False) -> str:
    grouped = build_city_groups()

    message_html = ""
    if message:
        cls = "error" if is_error else "success"
        message_html = f'<div class="message {cls}">{html.escape(message)}</div>'

    rows_html = ""
    for city_code, data in grouped.items():
        def _render_source(src: dict) -> str:
            display_url = (
                '<em style="color:#aaa">[encrypted]</em>'
                if src.get("source_encrypted")
                else html.escape(src["source_value"] or "")
            )
            status = "enabled" if src["is_enabled"] else "disabled"
            refresh = html.escape(str(src["refresh_interval_seconds"]) if src["refresh_interval_seconds"] is not None else "n/a")
            return (
                f'<div><span class="badge">{html.escape(src["feed_type"])}</span>'
                f'{html.escape(src["source_kind"] or "")} '
                f'{display_url} '
                f'({status}, refresh={refresh})</div>'
            )
        sources_html = "".join(_render_source(src) for src in data["sources"]) \
            or '<div class="muted">No GTFS-RT sources configured</div>'

        rows_html += f'''
        <tr>
          <td><strong>{html.escape(city_code)}</strong></td>
          <td>{html.escape(data["city_name"] or "")}</td>
          <td>{html.escape(data["timezone_name"] or "")}</td>
          <td>{html.escape(str(data["rollover_hour"]) if data["rollover_hour"] is not None else "")}</td>
          <td>
            <span class="badge {'enabled' if data["is_enabled"] else 'disabled'}">
              {'enabled' if data["is_enabled"] else 'disabled'}
            </span>
          </td>
          <td>{"yes" if data["sim_enabled"] else "no"}</td>
          <td>{html.escape(data["sim_force_date"] or "-")}</td>
          <td>{sources_html}</td>
          <td>
            <a href="/admin/cities/{html.escape(city_code)}/edit">Edit</a>
            <br>
            <a href="/admin/cities/{html.escape(city_code)}/gtfs">Update GTFS</a>
            <br>
            <form class="inline-form" action="/admin/cities/{html.escape(city_code)}/toggle-enabled" method="post">
              <button class="secondary-button" type="submit">
                {'Disable' if data["is_enabled"] else 'Enable'}
              </button>
            </form>
          </td>
        </tr>
        '''

    if not rows_html:
        rows_html = '<tr><td colspan="9">No cities configured yet.</td></tr>'

    body = f'''
    <h1>City Management</h1>
    {message_html}
    <div class="panel">
      <div style="margin-bottom: 12px;"><a href="/admin/cities/new">+ Import a new city</a></div>
      <table>
        <thead>
          <tr>
            <th>Code</th>
            <th>Name</th>
            <th>Timezone</th>
            <th>Rollover</th>
            <th>City enabled</th>
            <th>Sim enabled</th>
            <th>Forced sim date</th>
            <th>GTFS-RT sources</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>
    '''
    return render_layout("City Management", body)



def render_import_job_script(form_id: str, log_panel_id: str, log_box_id: str) -> str:
    return f'''
    <script>
      (() => {{
        const form = document.getElementById("{form_id}");
        const logPanel = document.getElementById("{log_panel_id}");
        const logBox = document.getElementById("{log_box_id}");

        if (!form || !logPanel || !logBox) return;

        let pollTimer = null;

        async function pollJob(jobId) {{
          const response = await fetch(`/admin/import/jobs/${{jobId}}`, {{ cache: "no-store" }});
          if (!response.ok) {{
            logBox.textContent += "\\nFailed to load job status.";
            return;
          }}

          const data = await response.json();
          logBox.textContent = data.logs || "";
          logBox.scrollTop = logBox.scrollHeight;

          if (!data.done) {{
            pollTimer = window.setTimeout(() => pollJob(jobId), 1000);
            return;
          }}

          logBox.textContent += data.ok
            ? "\\nDONE: loader_exit_code=" + data.exit_code
            : "\\nFAILED";
          logBox.scrollTop = logBox.scrollHeight;
        }}

        form.addEventListener("submit", async (event) => {{
          event.preventDefault();

          if (pollTimer) window.clearTimeout(pollTimer);

          logPanel.style.display = "block";
          logBox.textContent = "Uploading GTFS data...\\n";

          const submitButton = form.querySelector("button[type='submit']");
          if (submitButton) submitButton.disabled = true;

          try {{
            const response = await fetch(form.action, {{
              method: "POST",
              body: new FormData(form),
            }});

            const data = await response.json();

            if (!response.ok || data.error) {{
              logBox.textContent += "Failed to start import: " + (data.error || response.statusText);
              return;
            }}

            logBox.textContent += "Started job " + data.job_id + "\\n";
            await pollJob(data.job_id);
          }} catch (error) {{
            logBox.textContent += "Request failed: " + String(error);
          }} finally {{
            if (submitButton) submitButton.disabled = false;
          }}
        }});
      }})();
    </script>
    '''


def render_import_page(message: str = "", is_error: bool = False) -> str:
    message_html = ""
    if message:
        cls = "error" if is_error else "success"
        message_html = f'<div class="message {cls}">{html.escape(message)}</div>'

    body = f'''
    <h1>Import City</h1>
    <div class="panel">
      {message_html}
      <form id="city-import-form" action="/admin/cities/import" method="post" enctype="multipart/form-data">
        <label for="city_code">City code</label>
        <input type="text" id="city_code" name="city_code" placeholder="sofia" required>

        <label for="city_name">City name</label>
        <input type="text" id="city_name" name="city_name" placeholder="Sofia" required>

        <label for="timezone_name">Timezone</label>
        <input type="text" id="timezone_name" name="timezone_name" value="Europe/Sofia" required>

        <label for="service_day_rollover_hour">Service day rollover hour</label>
        <input type="number" id="service_day_rollover_hour" name="service_day_rollover_hour" value="4" min="0" max="12" required>

        <label>GTFS static data source</label>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
          <label style="font-weight:normal"><input type="radio" name="gtfs_source_type" value="file" checked onchange="document.getElementById('gtfs_file_row').style.display='';document.getElementById('gtfs_url_row').style.display='none'"> Upload file (.zip or .rar)</label>
          <label style="font-weight:normal"><input type="radio" name="gtfs_source_type" value="url" onchange="document.getElementById('gtfs_file_row').style.display='none';document.getElementById('gtfs_url_row').style.display=''"> Download from URL</label>
        </div>
        <div id="gtfs_file_row">
          <input type="file" id="gtfs_zip" name="gtfs_zip" accept=".zip,.rar">
        </div>
        <div id="gtfs_url_row" style="display:none">
          <input type="text" id="gtfs_url" name="gtfs_url" placeholder="https://example.com/gtfs.zip">
        </div>

        <label for="vehicle_positions_url">Vehicle positions URL or file path</label>
        <input type="text" id="vehicle_positions_url" name="vehicle_positions_url" placeholder="https://...">
        <label for="vehicle_positions_refresh">Vehicle positions refresh seconds</label>
        <input type="number" id="vehicle_positions_refresh" name="vehicle_positions_refresh" value="15" min="1">
        <details style="margin:4px 0 8px 0">
          <summary style="cursor:pointer;color:var(--text-muted,#aaa);font-size:13px">API credentials for vehicle positions (optional)</summary>
          <div style="margin-top:6px">
            <label style="font-weight:normal;display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <input type="checkbox" name="vehicle_positions_encrypt_url" value="true">
              URL contains an API key — encrypt it at rest
            </label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" name="vehicle_positions_api_key" placeholder="API key value" style="flex:1" autocomplete="off">
              <select name="vehicle_positions_api_key_placement" style="flex-shrink:0">
                <option value="">— Not needed —</option>
                <option value="query">URL query param (?key=...)</option>
                <option value="header">HTTP header (X-Api-Key: ...)</option>
              </select>
              <input type="text" name="vehicle_positions_api_key_name" placeholder="Param/header name" style="width:160px" autocomplete="off">
            </div>
            <small style="color:var(--text-muted,#aaa)">Tick the checkbox if your URL already contains an API key and you want it stored encrypted. Or use the fields below to add a key separately.</small>
          </div>
        </details>

        <label for="trip_updates_url">Trip updates URL or file path</label>
        <input type="text" id="trip_updates_url" name="trip_updates_url" placeholder="https://...">
        <label for="trip_updates_refresh">Trip updates refresh seconds</label>
        <input type="number" id="trip_updates_refresh" name="trip_updates_refresh" value="30" min="1">
        <details style="margin:4px 0 8px 0">
          <summary style="cursor:pointer;color:var(--text-muted,#aaa);font-size:13px">API credentials for trip updates (optional)</summary>
          <div style="margin-top:6px">
            <label style="font-weight:normal;display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <input type="checkbox" name="trip_updates_encrypt_url" value="true">
              URL contains an API key — encrypt it at rest
            </label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" name="trip_updates_api_key" placeholder="API key value" style="flex:1" autocomplete="off">
              <select name="trip_updates_api_key_placement" style="flex-shrink:0">
                <option value="">— Not needed —</option>
                <option value="query">URL query param (?key=...)</option>
                <option value="header">HTTP header (X-Api-Key: ...)</option>
              </select>
              <input type="text" name="trip_updates_api_key_name" placeholder="Param/header name" style="width:160px" autocomplete="off">
            </div>
            <small style="color:var(--text-muted,#aaa)">Tick the checkbox if your URL already contains an API key and you want it stored encrypted. Or use the fields below to add a key separately.</small>
          </div>
        </details>

        <label for="alerts_url">Alerts URL or file path</label>
        <input type="text" id="alerts_url" name="alerts_url" placeholder="https://...">
        <label for="alerts_refresh">Alerts refresh seconds</label>
        <input type="number" id="alerts_refresh" name="alerts_refresh" value="60" min="1">
        <details style="margin:4px 0 8px 0">
          <summary style="cursor:pointer;color:var(--text-muted,#aaa);font-size:13px">API credentials for alerts (optional)</summary>
          <div style="margin-top:6px">
            <label style="font-weight:normal;display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <input type="checkbox" name="alerts_encrypt_url" value="true">
              URL contains an API key — encrypt it at rest
            </label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" name="alerts_api_key" placeholder="API key value" style="flex:1" autocomplete="off">
              <select name="alerts_api_key_placement" style="flex-shrink:0">
                <option value="">— Not needed —</option>
                <option value="query">URL query param (?key=...)</option>
                <option value="header">HTTP header (X-Api-Key: ...)</option>
              </select>
              <input type="text" name="alerts_api_key_name" placeholder="Param/header name" style="width:160px" autocomplete="off">
            </div>
            <small style="color:var(--text-muted,#aaa)">Tick the checkbox if your URL already contains an API key and you want it stored encrypted. Or use the fields below to add a key separately.</small>
          </div>
        </details>

        <button type="submit">Import city</button>
      </form>
      <div id="city-import-log-panel" class="import-log-panel">
        <h3>Import logs</h3>
        <pre id="city-import-log-box" class="import-log-box"></pre>
      </div>
      {render_import_job_script("city-import-form", "city-import-log-panel", "city-import-log-box")}
    </div>
    '''
    return render_layout("Import City", body)


def render_gtfs_update_page(city: dict, message: str = "", is_error: bool = False) -> str:
    message_html = ""
    if message:
        cls = "error" if is_error else "success"
        message_html = f'<div class="message {cls}">{html.escape(message)}</div>'

    body = f"""
    <h1>Update GTFS: {html.escape(city["city_code"])}</h1>
    <div class="panel">
      {message_html}
      <p class="muted">
        Upload a new GTFS zip/rar or provide a download URL. Existing city settings and GTFS-RT source settings will be preserved.
        The loader runs with <strong>--replace</strong>, so static GTFS tables for this city will be replaced.
      </p>

      <form id="gtfs-update-form" action="/admin/cities/{html.escape(city["city_code"])}/gtfs" method="post" enctype="multipart/form-data">
        <label>City</label>
        <div>
          <strong>{html.escape(city["city_name"])}</strong>
          <span class="muted">({html.escape(city["city_code"])})</span>
        </div>

        <label>Timezone</label>
        <div>{html.escape(city["timezone_name"])}</div>

        <label>Service day rollover hour</label>
        <div>{html.escape(str(city["service_day_rollover_hour"]))}</div>

        <label>GTFS static data source</label>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
          <label style="font-weight:normal"><input type="radio" name="gtfs_source_type" value="file" checked onchange="document.getElementById('gtfs_file_row').style.display='';document.getElementById('gtfs_url_row').style.display='none'"> Upload file (.zip or .rar)</label>
          <label style="font-weight:normal"><input type="radio" name="gtfs_source_type" value="url" onchange="document.getElementById('gtfs_file_row').style.display='none';document.getElementById('gtfs_url_row').style.display=''"> Download from URL</label>
        </div>
        <div id="gtfs_file_row">
          <input type="file" id="gtfs_zip" name="gtfs_zip" accept=".zip,.rar">
        </div>
        <div id="gtfs_url_row" style="display:none">
          <input type="text" id="gtfs_url" name="gtfs_url" placeholder="https://example.com/gtfs.zip">
        </div>

        <button type="submit">Update GTFS</button>
      </form>

      <p class="muted" style="margin-top: 16px;">
        Recommended: stop the runtime before updating GTFS, then start it again after the upload finishes.
      </p>
      <div id="gtfs-update-log-panel" class="import-log-panel">
        <h3>Update logs</h3>
        <pre id="gtfs-update-log-box" class="import-log-box"></pre>
      </div>
      {render_import_job_script("gtfs-update-form", "gtfs-update-log-panel", "gtfs-update-log-box")}
    </div>
    """
    return render_layout(f'Update GTFS - {city["city_code"]}', body)


def render_edit_page(city: dict, message: str = "", is_error: bool = False) -> str:
    message_html = ""
    if message:
        cls = "error" if is_error else "success"
        message_html = f'<div class="message {cls}">{html.escape(message)}</div>'

    def src(feed_type: str):
        return city["sources"].get(feed_type, {
            "source_value": "",
            "is_enabled": False,
            "refresh_interval_seconds": "",
        })

    vehicle_positions = src("vehicle_positions")
    trip_updates = src("trip_updates")
    alerts = src("alerts")

    checked_city_enabled = "checked" if city["is_enabled"] else ""
    checked_sim_enabled = "checked" if city["sim_enabled"] else ""
    checked_vp = "checked" if vehicle_positions["is_enabled"] else ""
    checked_tu = "checked" if trip_updates["is_enabled"] else ""
    checked_al = "checked" if alerts["is_enabled"] else ""
    checked_enc_vp = "checked" if vehicle_positions.get("source_encrypted") else ""
    checked_enc_tu = "checked" if trip_updates.get("source_encrypted") else ""
    checked_enc_al = "checked" if alerts.get("source_encrypted") else ""
    gtfs_url_val = html.escape(city.get("gtfs_url") or "")
    gtfs_url_placeholder = "Stored encrypted URL — leave blank to keep existing" if city.get("gtfs_url_encrypted") else "https://example.com/gtfs.zip"
    gtfs_interval_val = html.escape(str(city.get("gtfs_refresh_interval_hours") or 24))
    checked_gtfs_url_encrypted = "checked" if city.get("gtfs_url_encrypted") else ""

    def source_placeholder(source: dict) -> str:
        return "Stored encrypted URL — leave blank to keep existing" if source.get("source_encrypted") else "https://example.com/gtfs-rt.pb"

    def runtime_value(name: str) -> str:
        value = city.get(name)
        return "" if value is None else html.escape(str(value))

    def env_default(env_name: str) -> str:
        value = os.getenv(env_name)
        if value is None or value == "":
            return ".env not set"
        return value

    def env_placeholder(env_name: str) -> str:
        return html.escape(env_default(env_name), quote=True)

    def bool_option(name: str, value: str) -> str:
        current = city.get(name)
        if value == "" and current is None:
            return "selected"
        if value == "true" and current is True:
            return "selected"
        if value == "false" and current is False:
            return "selected"
        return ""

    def field_hint(text: str) -> str:
        return (
            '<div class="muted" style="font-size:12px;margin:-2px 0 6px 0">'
            + html.escape(text)
            + '</div>'
        )

    runtime_overrides_html = f'''
        <details style="margin:18px 0 20px 0;border:1px solid #e5e7eb;border-radius:10px;padding:12px;background:#fafafa">
          <summary style="cursor:pointer;font-weight:bold">Advanced runtime settings</summary>
          <p class="muted" style="margin-top:8px">Leave fields empty to use the shown <code>.env</code> values.</p>

          <h3>Simulation</h3>
          <label for="sim_interval_seconds">SIM_INTERVAL_SECONDS</label>
          {field_hint("How often the simulation recalculates scheduled vehicle positions for this city.")}
          <input type="number" id="sim_interval_seconds" name="sim_interval_seconds" value="{runtime_value('sim_interval_seconds')}" min="1" placeholder="{env_placeholder('SIM_INTERVAL_SECONDS')}">

          <label for="sim_active_trip_lookbehind_seconds">SIM_ACTIVE_TRIP_LOOKBEHIND_SECONDS</label>
          {field_hint("How far back from the current service time trips can still be treated as active.")}
          <input type="number" id="sim_active_trip_lookbehind_seconds" name="sim_active_trip_lookbehind_seconds" value="{runtime_value('sim_active_trip_lookbehind_seconds')}" min="0" placeholder="{env_placeholder('SIM_ACTIVE_TRIP_LOOKBEHIND_SECONDS')}">

          <label for="sim_active_trip_lookahead_seconds">SIM_ACTIVE_TRIP_LOOKAHEAD_SECONDS</label>
          {field_hint("How far ahead trips are loaded before they start, so simulation can prepare active vehicles.")}
          <input type="number" id="sim_active_trip_lookahead_seconds" name="sim_active_trip_lookahead_seconds" value="{runtime_value('sim_active_trip_lookahead_seconds')}" min="0" placeholder="{env_placeholder('SIM_ACTIVE_TRIP_LOOKAHEAD_SECONDS')}">

          <label for="sim_apply_trip_updates">SIM_APPLY_TRIP_UPDATES</label>
          {field_hint("Whether GTFS-RT trip update delays should adjust the simulated schedule when available.")}
          <select id="sim_apply_trip_updates" name="sim_apply_trip_updates">
            <option value="" {bool_option('sim_apply_trip_updates', '')}>Use .env default ({html.escape(env_default('SIM_APPLY_TRIP_UPDATES'))})</option>
            <option value="true" {bool_option('sim_apply_trip_updates', 'true')}>true</option>
            <option value="false" {bool_option('sim_apply_trip_updates', 'false')}>false</option>
          </select>

          <h3>Fusion</h3>
          <label for="fusion_interval_seconds">FUSION_INTERVAL_SECONDS</label>
          {field_hint("How often fusion rebuilds the current vehicle state and publishes the city snapshot.")}
          <input type="number" id="fusion_interval_seconds" name="fusion_interval_seconds" value="{runtime_value('fusion_interval_seconds')}" min="1" placeholder="{env_placeholder('FUSION_INTERVAL_SECONDS')}">

          <label for="fusion_rt_max_age_seconds">FUSION_RT_MAX_AGE_SECONDS</label>
          {field_hint("Maximum age of realtime vehicle positions before they are ignored as stale.")}
          <input type="number" id="fusion_rt_max_age_seconds" name="fusion_rt_max_age_seconds" value="{runtime_value('fusion_rt_max_age_seconds')}" min="1" placeholder="{env_placeholder('FUSION_RT_MAX_AGE_SECONDS')}">

          <label for="fusion_min_match_score">FUSION_MIN_MATCH_SCORE</label>
          {field_hint("Minimum score needed before an RT vehicle is accepted as matched to a scheduled trip.")}
          <input type="number" id="fusion_min_match_score" name="fusion_min_match_score" value="{runtime_value('fusion_min_match_score')}" step="0.1" placeholder="{env_placeholder('FUSION_MIN_MATCH_SCORE')}">

          <label for="fusion_max_shape_distance_m">FUSION_MAX_SHAPE_DISTANCE_M</label>
          {field_hint("Maximum allowed distance from a route shape when checking whether a vehicle position is plausible.")}
          <input type="number" id="fusion_max_shape_distance_m" name="fusion_max_shape_distance_m" value="{runtime_value('fusion_max_shape_distance_m')}" step="0.1" min="0" placeholder="{env_placeholder('FUSION_MAX_SHAPE_DISTANCE_M')}">

          <h3>Stop arrivals</h3>
          <label for="stop_arrivals_interval_seconds">STOP_ARRIVALS_INTERVAL_SECONDS</label>
          {field_hint("How often stop arrival predictions are recalculated and published.")}
          <input type="number" id="stop_arrivals_interval_seconds" name="stop_arrivals_interval_seconds" value="{runtime_value('stop_arrivals_interval_seconds')}" min="1" placeholder="{env_placeholder('STOP_ARRIVALS_INTERVAL_SECONDS')}">

          <label for="stop_arrivals_horizon_seconds">STOP_ARRIVALS_HORIZON_SECONDS</label>
          {field_hint("How far into the future arrivals are included for each stop.")}
          <input type="number" id="stop_arrivals_horizon_seconds" name="stop_arrivals_horizon_seconds" value="{runtime_value('stop_arrivals_horizon_seconds')}" min="1" placeholder="{env_placeholder('STOP_ARRIVALS_HORIZON_SECONDS')}">

          <label for="stop_arrivals_limit_per_stop">STOP_ARRIVALS_LIMIT_PER_STOP</label>
          {field_hint("Maximum number of arrivals kept for a single stop in one published snapshot.")}
          <input type="number" id="stop_arrivals_limit_per_stop" name="stop_arrivals_limit_per_stop" value="{runtime_value('stop_arrivals_limit_per_stop')}" min="1" placeholder="{env_placeholder('STOP_ARRIVALS_LIMIT_PER_STOP')}">

          <label for="stop_arrivals_shape_speed_mps">STOP_ARRIVALS_SHAPE_SPEED_MPS</label>
          {field_hint("Fallback speed used for shape-based arrival estimates when better timing data is missing.")}
          <input type="number" id="stop_arrivals_shape_speed_mps" name="stop_arrivals_shape_speed_mps" value="{runtime_value('stop_arrivals_shape_speed_mps')}" step="0.1" min="0" placeholder="{env_placeholder('STOP_ARRIVALS_SHAPE_SPEED_MPS')}">

          <label for="stop_arrivals_shape_max_distance_m">STOP_ARRIVALS_SHAPE_MAX_DISTANCE_M</label>
          {field_hint("Maximum distance used when matching vehicles or stops to route shapes for arrival calculation.")}
          <input type="number" id="stop_arrivals_shape_max_distance_m" name="stop_arrivals_shape_max_distance_m" value="{runtime_value('stop_arrivals_shape_max_distance_m')}" step="0.1" min="0" placeholder="{env_placeholder('STOP_ARRIVALS_SHAPE_MAX_DISTANCE_M')}">

          <label for="stop_arrivals_shape_passed_grace_m">STOP_ARRIVALS_SHAPE_PASSED_GRACE_M</label>
          {field_hint("Grace distance after a stop before the vehicle is treated as having already passed it.")}
          <input type="number" id="stop_arrivals_shape_passed_grace_m" name="stop_arrivals_shape_passed_grace_m" value="{runtime_value('stop_arrivals_shape_passed_grace_m')}" step="0.1" min="0" placeholder="{env_placeholder('STOP_ARRIVALS_SHAPE_PASSED_GRACE_M')}">
        </details>
    '''

    body = f'''
    <h1>Edit city: {html.escape(city["city_code"])}</h1>
    <div class="panel">
      {message_html}
      <form action="/admin/cities/{html.escape(city["city_code"])}/edit" method="post">
        <label for="city_name">City name</label>
        <input type="text" id="city_name" name="city_name" value="{html.escape(city["city_name"])}" required>

        <label for="timezone_name">Timezone</label>
        <input type="text" id="timezone_name" name="timezone_name" value="{html.escape(city["timezone_name"])}" required>

        <label for="service_day_rollover_hour">Service day rollover hour</label>
        <input type="number" id="service_day_rollover_hour" name="service_day_rollover_hour" value="{html.escape(str(city["service_day_rollover_hour"]))}" min="0" max="12" required>

        <label><input type="checkbox" name="is_enabled" value="true" {checked_city_enabled}> City enabled</label>
        <label><input type="checkbox" name="sim_enabled" value="true" {checked_sim_enabled}> Simulation enabled</label>

        <label for="sim_force_date">Forced simulation date (YYYY-MM-DD)</label>
        <input type="text" id="sim_force_date" name="sim_force_date" value="{html.escape(city["sim_force_date"])}" placeholder="2022-03-15">

        <label for="sim_force_time">Forced simulation time (HH:MM:SS, optional)</label>
        <input type="text" id="sim_force_time" name="sim_force_time" value="{html.escape(city["sim_force_time"])}" placeholder="14:30:00">

        <h2>GTFS-RT sources</h2>

        <label><input type="checkbox" name="vehicle_positions_enabled" value="true" {checked_vp}> Vehicle positions enabled</label>
        <label for="vehicle_positions_source_value">Vehicle positions URL or file path</label>
        <input type="text" id="vehicle_positions_source_value" name="vehicle_positions_source_value" value="{html.escape(vehicle_positions["source_value"] or "")}" placeholder="{html.escape(source_placeholder(vehicle_positions))}">
        <label for="vehicle_positions_refresh_interval_seconds">Vehicle positions refresh seconds</label>
        <input type="number" id="vehicle_positions_refresh_interval_seconds" name="vehicle_positions_refresh_interval_seconds" value="{html.escape(str(vehicle_positions["refresh_interval_seconds"] or 15))}" min="1">
        <details style="margin:4px 0 8px 0">
          <summary style="cursor:pointer;color:var(--text-muted,#aaa);font-size:13px">API credentials for vehicle positions (optional)</summary>
          <div style="margin-top:6px">
            <label style="font-weight:normal;display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <input type="checkbox" name="vehicle_positions_encrypt_url_edit" value="true" {checked_enc_vp}>
              URL contains an API key — encrypt it at rest
            </label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" name="vehicle_positions_api_key" placeholder="API key value" style="flex:1" autocomplete="off">
              <select name="vehicle_positions_api_key_placement" style="flex-shrink:0">
                <option value="">— Not needed —</option>
                <option value="query">URL query param (?key=...)</option>
                <option value="header">HTTP header (X-Api-Key: ...)</option>
              </select>
              <input type="text" name="vehicle_positions_api_key_name" placeholder="Param/header name" style="width:160px" autocomplete="off">
            </div>
            <small style="color:var(--text-muted,#aaa)">Tick to encrypt the URL (which contains your API key). Untick to store it plaintext.</small>
          </div>
        </details>

        <label><input type="checkbox" name="trip_updates_enabled" value="true" {checked_tu}> Trip updates enabled</label>
        <label for="trip_updates_source_value">Trip updates URL or file path</label>
        <input type="text" id="trip_updates_source_value" name="trip_updates_source_value" value="{html.escape(trip_updates["source_value"] or "")}" placeholder="{html.escape(source_placeholder(trip_updates))}">
        <label for="trip_updates_refresh_interval_seconds">Trip updates refresh seconds</label>
        <input type="number" id="trip_updates_refresh_interval_seconds" name="trip_updates_refresh_interval_seconds" value="{html.escape(str(trip_updates["refresh_interval_seconds"] or 30))}" min="1">
        <details style="margin:4px 0 8px 0">
          <summary style="cursor:pointer;color:var(--text-muted,#aaa);font-size:13px">API credentials for trip updates (optional)</summary>
          <div style="margin-top:6px">
            <label style="font-weight:normal;display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <input type="checkbox" name="trip_updates_encrypt_url_edit" value="true" {checked_enc_tu}>
              URL contains an API key — encrypt it at rest
            </label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" name="trip_updates_api_key" placeholder="API key value" style="flex:1" autocomplete="off">
              <select name="trip_updates_api_key_placement" style="flex-shrink:0">
                <option value="">— Not needed —</option>
                <option value="query">URL query param (?key=...)</option>
                <option value="header">HTTP header (X-Api-Key: ...)</option>
              </select>
              <input type="text" name="trip_updates_api_key_name" placeholder="Param/header name" style="width:160px" autocomplete="off">
            </div>
            <small style="color:var(--text-muted,#aaa)">Tick to encrypt the URL (which contains your API key). Untick to store it plaintext.</small>
          </div>
        </details>

        <label><input type="checkbox" name="alerts_enabled" value="true" {checked_al}> Alerts enabled</label>
        <label for="alerts_source_value">Alerts URL or file path</label>
        <input type="text" id="alerts_source_value" name="alerts_source_value" value="{html.escape(alerts["source_value"] or "")}" placeholder="{html.escape(source_placeholder(alerts))}">
        <label for="alerts_refresh_interval_seconds">Alerts refresh seconds</label>
        <input type="number" id="alerts_refresh_interval_seconds" name="alerts_refresh_interval_seconds" value="{html.escape(str(alerts["refresh_interval_seconds"] or 60))}" min="1">
        <details style="margin:4px 0 8px 0">
          <summary style="cursor:pointer;color:var(--text-muted,#aaa);font-size:13px">API credentials for alerts (optional)</summary>
          <div style="margin-top:6px">
            <label style="font-weight:normal;display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <input type="checkbox" name="alerts_encrypt_url_edit" value="true" {checked_enc_al}>
              URL contains an API key — encrypt it at rest
            </label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" name="alerts_api_key" placeholder="API key value" style="flex:1" autocomplete="off">
              <select name="alerts_api_key_placement" style="flex-shrink:0">
                <option value="">— Not needed —</option>
                <option value="query">URL query param (?key=...)</option>
                <option value="header">HTTP header (X-Api-Key: ...)</option>
              </select>
              <input type="text" name="alerts_api_key_name" placeholder="Param/header name" style="width:160px" autocomplete="off">
            </div>
            <small style="color:var(--text-muted,#aaa)">Tick to encrypt the URL (which contains your API key). Untick to store it plaintext.</small>
          </div>
        </details>

        <h2>GTFS Static Auto-Refresh</h2>
        <label for="gtfs_url">GTFS static feed URL (leave empty to disable auto-refresh)</label>
        <input type="text" id="gtfs_url" name="gtfs_url" value="{gtfs_url_val}" placeholder="{html.escape(gtfs_url_placeholder)}">
        <label><input type="checkbox" name="gtfs_url_encrypted" value="true" {checked_gtfs_url_encrypted}> Encrypt URL (use if URL contains credentials/tokens)</label>
        <small style="color:var(--text-muted,#aaa)">The feed will be downloaded, validated, and imported on the configured interval. Existing data is never touched if download or validation fails.</small>

        <label for="gtfs_refresh_interval_hours">Refresh interval (hours)</label>
        <small style="display:block;color:var(--text-muted,#aaa);margin:-2px 0 6px 0">How often the static GTFS data should be downloaded and re-imported. Leave the URL empty to disable auto-refresh.</small>
        <input type="number" id="gtfs_refresh_interval_hours" name="gtfs_refresh_interval_hours" value="{gtfs_interval_val}" min="1" max="168">

        {runtime_overrides_html}

        <button type="submit">Save city settings</button>
      </form>
    </div>
    '''
    return render_layout(f'Edit city - {city["city_code"]}', body)