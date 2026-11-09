import hashlib
import hmac
import os
import secrets
from typing import Optional
import urllib.request as _urllib_request

from fastapi import APIRouter, BackgroundTasks, Cookie, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.api.db import DB_CONFIG, get_db_connection
from app.api.pool import get_conn
from app.api.views import render_admin_home, render_city_list_page, render_edit_page, render_gtfs_update_page, render_import_page
from transit.services.city_admin_service import get_city_detail, update_city_runtime_settings
from transit.services.city_import_service import import_city_bundle



_ADMIN_COOKIE_NAME = "admin_token"
_ADMIN_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days


def _get_secret_token() -> str:
    token = os.getenv("ADMIN_SECRET_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ADMIN_SECRET_TOKEN env var is not set")
    return token


def _make_cookie_value(raw_token: str) -> str:
    """HMAC-sign the token so the cookie can't be forged."""
    key = _get_secret_token().encode()
    return hmac.new(key, raw_token.encode(), hashlib.sha256).hexdigest()


def _check_auth(request: Request):
    """Return RedirectResponse if the admin cookie is missing or invalid, else None."""
    cookie = request.cookies.get(_ADMIN_COOKIE_NAME, "")
    try:
        expected = _make_cookie_value(_get_secret_token())
    except RuntimeError:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET_TOKEN not configured")
    if not cookie or not secrets.compare_digest(cookie, expected):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def _is_authenticated(request: Request) -> bool:
    result = _check_auth(request)
    return result is None


def _auth_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=303)


def _parse_api_key_fields(api_key: str, placement: str, name: str) -> tuple[dict, dict]:
    """Convert simple API key form fields into (headers, query_params) dicts."""
    api_key = (api_key or "").strip()
    placement = (placement or "").strip()
    name = (name or "").strip()
    if not api_key or not placement or not name:
        return {}, {}
    if placement == "header":
        return {name: api_key}, {}
    if placement == "query":
        return {}, {name: api_key}
    return {}, {}


async def _resolve_gtfs_upload(
    gtfs_zip: Optional[UploadFile],
    gtfs_url: str,
    city_code: str,
) -> tuple[bytes, str, str | None]:
    """Return (zip_bytes, filename, error). Handles upload, URL download, and rar extraction."""
    import io, zipfile

    raw_bytes = b""
    filename = f"{city_code}_gtfs.zip"
    if gtfs_zip and gtfs_zip.filename:
        raw_bytes = await gtfs_zip.read()
        filename = gtfs_zip.filename or filename
    elif gtfs_url:
        try:
            with _urllib_request.urlopen(gtfs_url, timeout=60) as resp:
                    raw_bytes = resp.read()
                    filename = gtfs_url.split("/")[-1].split("?")[0] or filename
        except Exception as e:
            return b"", filename, f"Failed to download GTFS from URL: {e}"
    else:
        return b"", filename, "Provide either a GTFS file upload or a URL."

    if not raw_bytes:
        return b"", filename, "GTFS file is empty."


    # Handle RAR files — extract and find the zip inside, or repack CSVs as zip
    if filename.lower().endswith(".rar") or raw_bytes[:4] == b"Rar!":
        try:
            import rarfile
            with rarfile.RarFile(io.BytesIO(raw_bytes)) as rf:
                names = rf.namelist()
                # If there's a zip inside the rar, use it directly
                zip_names = [n for n in names if n.lower().endswith(".zip")]
                if zip_names:
                    raw_bytes = rf.read(zip_names[0])
                    filename = zip_names[0]
                else:
                    # Repack all files as a zip
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        for name in names:
                            zf.writestr(name, rf.read(name))
                    raw_bytes = buf.getvalue()
                    filename = f"{city_code}_gtfs.zip"
        except ImportError:
            return b"", filename, "RAR support requires the 'rarfile' package. Run: pip install rarfile"
        except Exception as e:
            return b"", filename, f"Failed to extract RAR: {e}"

    # Validate it's a zip
    if not zipfile.is_zipfile(io.BytesIO(raw_bytes)):
        return b"", filename, "File is not a valid ZIP archive."

    return raw_bytes, filename, None

router = APIRouter()


# ---------------------------------------------------------------------------
# Import job helpers — thin wrappers over the DB-backed job_store
# ---------------------------------------------------------------------------

from app.api import job_store as _jobs  # noqa: E402


def _new_import_job(title: str) -> str:
    return _jobs.create_job(title)


def _append_import_job_log(job_id: str, message: str) -> None:
    _jobs.append_log(job_id, message)


def _finish_import_job(job_id: str, *, ok: bool, exit_code: int | None = None, message: str = "") -> None:
    _jobs.finish_job(job_id, ok=ok, exit_code=exit_code, message=message)


def _run_city_import_job(
    job_id: str,
    *,
    city_code: str,
    city_name: str,
    timezone_name: str,
    service_day_rollover_hour: int,
    zip_filename: str,
    zip_bytes: bytes,
    rt_sources: dict,
) -> None:
    try:
        _jobs.set_running(job_id)
        _append_import_job_log(job_id, f"Starting GTFS import city={city_code} name={city_name} file={zip_filename}")
        _append_import_job_log(job_id, "Saving upload and preparing city runtime config...")
        _append_import_job_log(job_id, "Running GTFS loader. This can take a while...")

        result = import_city_bundle(
            db_config=DB_CONFIG,
            city_code=city_code,
            city_name=city_name,
            timezone_name=timezone_name,
            service_day_rollover_hour=service_day_rollover_hour,
            zip_filename=zip_filename,
            zip_bytes=zip_bytes,
            rt_sources=rt_sources,
            log_callback=lambda line: _append_import_job_log(job_id, line),
        )

        if result.get("loader_stderr"):
            _append_import_job_log(job_id, "loader stderr:")
            _jobs.append_raw(job_id, str(result["loader_stderr"]) + "\n")

        _finish_import_job(
            job_id,
            ok=True,
            exit_code=int(result.get("loader_exit_code") or 0),
            message=(
                f'Imported city={result["city_code"]} name={result["city_name"]} '
                f'zip={result["saved_zip_path"]} '
                f'loader_exit_code={result["loader_exit_code"]}'
            ),
        )
    except Exception as e:
        _finish_import_job(job_id, ok=False, exit_code=None, message=str(e))


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: str = ""):
    if _is_authenticated(request):
        return RedirectResponse(url="/admin", status_code=303)
    error_html = f'<div style="color:#c00;margin-bottom:1em">{error}</div>' if error else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>Admin Login</title>
    <style>body{{font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f5f5f5}}
    .login-box{{background:#fff;padding:2rem;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);width:320px}}
    h2{{margin-top:0}}input{{width:100%;padding:.5rem;margin:.5rem 0 1rem;box-sizing:border-box;border:1px solid #ccc;border-radius:4px}}
    button{{width:100%;padding:.6rem;background:#2c3e50;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:1rem}}</style>
    </head><body><div class="login-box"><h2>Admin Login</h2>{error_html}
    <form method="post" action="/admin/login">
    <label>Token</label><input type="password" name="token" autofocus>
    <button type="submit">Sign in</button></form></div></body></html>""")


@router.post("/admin/login")
def admin_login(response: Response, token: str = Form("")):
    try:
        expected = _get_secret_token()
    except RuntimeError:
        resp = RedirectResponse(url="/admin/login?error=Server+configuration+error", status_code=303)
        resp.delete_cookie(_ADMIN_COOKIE_NAME)
        return resp
    if not secrets.compare_digest(token.strip(), expected):
        resp = RedirectResponse(url="/admin/login?error=Invalid+token", status_code=303)
        resp.delete_cookie(_ADMIN_COOKIE_NAME)
        return resp
    cookie_value = _make_cookie_value(expected)
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(_ADMIN_COOKIE_NAME, cookie_value, max_age=_ADMIN_COOKIE_MAX_AGE,
                    httponly=True, samesite="lax")
    return resp


@router.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(_ADMIN_COOKIE_NAME)
    return resp


@router.get("/admin/import/jobs/{job_id}")
def admin_import_job_status(request: Request, job_id: str):
    auth = _check_auth(request)
    if auth is not None: return auth
    job = _jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job



@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    auth = _check_auth(request)
    if auth is not None: return auth
    return render_admin_home()


@router.get("/admin/cities", response_class=HTMLResponse)
def admin_cities_page(request: Request):
    auth = _check_auth(request)
    if auth is not None: return auth
    return render_city_list_page()


@router.get("/admin/cities/new", response_class=HTMLResponse)
def admin_city_import_page(request: Request):
    auth = _check_auth(request)
    if auth is not None: return auth
    return render_import_page()


@router.post("/admin/cities/import")
async def admin_city_import(
    request: Request,
    background_tasks: BackgroundTasks,
    city_code: str = Form(...),
    city_name: str = Form(...),
    timezone_name: str = Form(...),
    service_day_rollover_hour: int = Form(...),
    gtfs_zip: Optional[UploadFile] = File(None),
    gtfs_url: str = Form(""),
    vehicle_positions_url: str = Form(""),
    vehicle_positions_refresh: int = Form(15),
    vehicle_positions_api_key: str = Form(""),
    vehicle_positions_api_key_placement: str = Form(""),
    vehicle_positions_api_key_name: str = Form(""),
    vehicle_positions_encrypt_url: str = Form(""),
    trip_updates_url: str = Form(""),
    trip_updates_refresh: int = Form(30),
    trip_updates_api_key: str = Form(""),
    trip_updates_api_key_placement: str = Form(""),
    trip_updates_api_key_name: str = Form(""),
    trip_updates_encrypt_url: str = Form(""),
    alerts_url: str = Form(""),
    alerts_refresh: int = Form(60),
    alerts_api_key: str = Form(""),
    alerts_api_key_placement: str = Form(""),
    alerts_api_key_name: str = Form(""),
    alerts_encrypt_url: str = Form(""),
):
    auth = _check_auth(request)
    if auth is not None: return auth
    clean_city_code = city_code.strip()
    clean_city_name = city_name.strip()
    gtfs_url = gtfs_url.strip()

    zip_bytes, zip_filename, err = await _resolve_gtfs_upload(gtfs_zip, gtfs_url, clean_city_code)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    job_id = _new_import_job(f"import city={clean_city_code}")

    background_tasks.add_task(
        _run_city_import_job,
        job_id,
        city_code=clean_city_code,
        city_name=clean_city_name,
        timezone_name=timezone_name.strip(),
        service_day_rollover_hour=service_day_rollover_hour,
        zip_filename=zip_filename,
        zip_bytes=zip_bytes,
        rt_sources={
            "vehicle_positions": {
                "source_value": vehicle_positions_url.strip(),
                "refresh_interval_seconds": vehicle_positions_refresh,
                "encrypt_url": bool(vehicle_positions_encrypt_url),
                **dict(zip(("headers", "query_params"), _parse_api_key_fields(vehicle_positions_api_key, vehicle_positions_api_key_placement, vehicle_positions_api_key_name))),
            },
            "trip_updates": {
                "source_value": trip_updates_url.strip(),
                "refresh_interval_seconds": trip_updates_refresh,
                "encrypt_url": bool(trip_updates_encrypt_url),
                **dict(zip(("headers", "query_params"), _parse_api_key_fields(trip_updates_api_key, trip_updates_api_key_placement, trip_updates_api_key_name))),
            },
            "alerts": {
                "source_value": alerts_url.strip(),
                "refresh_interval_seconds": alerts_refresh,
                "encrypt_url": bool(alerts_encrypt_url),
                **dict(zip(("headers", "query_params"), _parse_api_key_fields(alerts_api_key, alerts_api_key_placement, alerts_api_key_name))),
            },
        },
    )

    return {"job_id": job_id}


def _city_sources_for_import(city: dict) -> dict:
    sources = city.get("sources", {})

    def source_payload(feed_type: str, default_refresh: int) -> dict:
        src = sources.get(feed_type, {})
        if not src.get("is_enabled") or not src.get("source_value"):
            return {
                "source_value": "",
                "refresh_interval_seconds": default_refresh,
            }

        return {
            "source_value": str(src.get("source_value") or ""),
            "refresh_interval_seconds": int(src.get("refresh_interval_seconds") or default_refresh),
        }

    return {
        "vehicle_positions": source_payload("vehicle_positions", 15),
        "trip_updates": source_payload("trip_updates", 30),
        "alerts": source_payload("alerts", 60),
    }



@router.post("/admin/cities/{city_code}/toggle-enabled", response_class=HTMLResponse)
def admin_city_toggle_enabled(request: Request, city_code: str):
    auth = _check_auth(request)
    if auth is not None: return auth
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT city_id FROM cities WHERE city_code = %s",
                (city_code,),
            ).fetchone()

            if row is None:
                raise HTTPException(status_code=404, detail="City not found")

            city_id = int(row[0])

            conn.execute(
                """
                INSERT INTO city_runtime_config (
                    city_id, timezone_name, service_day_rollover_hour, is_enabled, sim_enabled
                )
                VALUES (
                    %s,
                    COALESCE((SELECT timezone FROM cities WHERE city_id = %s), 'UTC'),
                    4,
                    FALSE,
                    TRUE
                )
                ON CONFLICT (city_id) DO UPDATE
                SET is_enabled = NOT COALESCE(city_runtime_config.is_enabled, TRUE)
                """,
                (city_id, city_id),
            )
            conn.commit()

        return render_city_list_page(message=f"Toggled city: {city_code}", is_error=False)
    except HTTPException:
        raise
    except Exception as e:
        return render_city_list_page(message=str(e), is_error=True)



@router.get("/admin/cities/{city_code}/gtfs", response_class=HTMLResponse)
def admin_city_gtfs_update_page(request: Request, city_code: str):
    auth = _check_auth(request)
    if auth is not None: return auth
    with get_conn() as conn:
        city = get_city_detail(conn, city_code)
    if city is None:
        raise HTTPException(status_code=404, detail="City not found")
    return render_gtfs_update_page(city)


@router.post("/admin/cities/{city_code}/gtfs")
async def admin_city_gtfs_update_save(
    request: Request,
    background_tasks: BackgroundTasks,
    city_code: str,
    gtfs_zip: Optional[UploadFile] = File(None),
    gtfs_url: str = Form(""),
):
    auth = _check_auth(request)
    if auth is not None: return auth
    with get_conn() as conn:
        city = get_city_detail(conn, city_code)

    if city is None:
        raise HTTPException(status_code=404, detail="City not found")

    gtfs_url = gtfs_url.strip()
    zip_bytes, zip_filename, err = await _resolve_gtfs_upload(gtfs_zip, gtfs_url, city["city_code"])
    if err:
        return JSONResponse({"error": err}, status_code=400)

    job_id = _new_import_job(f"update GTFS city={city['city_code']}")

    background_tasks.add_task(
        _run_city_import_job,
        job_id,
        city_code=city["city_code"],
        city_name=city["city_name"],
        timezone_name=city["timezone_name"],
        service_day_rollover_hour=city["service_day_rollover_hour"],
        zip_filename=zip_filename,
        zip_bytes=zip_bytes,
        rt_sources=_city_sources_for_import(city),
    )

    return {"job_id": job_id}



@router.get("/admin/cities/{city_code}/edit", response_class=HTMLResponse)
def admin_city_edit_page(request: Request, city_code: str):
    auth = _check_auth(request)
    if auth is not None: return auth
    with get_conn() as conn:
        city = get_city_detail(conn, city_code)
    if city is None:
        raise HTTPException(status_code=404, detail="City not found")
    return render_edit_page(city)


@router.post("/admin/cities/{city_code}/edit", response_class=HTMLResponse)
def admin_city_edit_save(
    request: Request,
    city_code: str,
    city_name: str = Form(...),
    timezone_name: str = Form(...),
    service_day_rollover_hour: int = Form(...),
    is_enabled: str | None = Form(None),
    sim_enabled: str | None = Form(None),
    sim_force_date: str = Form(""),
    sim_force_time: str = Form(""),
    vehicle_positions_enabled: str | None = Form(None),
    vehicle_positions_source_value: str = Form(""),
    vehicle_positions_refresh_interval_seconds: int = Form(15),
    vehicle_positions_encrypt_url_edit: str = Form(""),
    trip_updates_enabled: str | None = Form(None),
    trip_updates_source_value: str = Form(""),
    trip_updates_refresh_interval_seconds: int = Form(30),
    trip_updates_encrypt_url_edit: str = Form(""),
    alerts_enabled: str | None = Form(None),
    alerts_source_value: str = Form(""),
    alerts_refresh_interval_seconds: int = Form(60),
    alerts_encrypt_url_edit: str = Form(""),
    gtfs_url: str = Form(""),
    gtfs_refresh_interval_hours: int = Form(24),
    gtfs_url_encrypted: str | None = Form(None),
    sim_interval_seconds: str = Form(""),
    sim_active_trip_lookbehind_seconds: str = Form(""),
    sim_active_trip_lookahead_seconds: str = Form(""),
    sim_apply_trip_updates: str = Form(""),
    fusion_interval_seconds: str = Form(""),
    fusion_rt_max_age_seconds: str = Form(""),
    fusion_min_match_score: str = Form(""),
    fusion_max_shape_distance_m: str = Form(""),
    stop_arrivals_interval_seconds: str = Form(""),
    stop_arrivals_horizon_seconds: str = Form(""),
    stop_arrivals_limit_per_stop: str = Form(""),
    stop_arrivals_shape_speed_mps: str = Form(""),
    stop_arrivals_shape_max_distance_m: str = Form(""),
    stop_arrivals_shape_passed_grace_m: str = Form(""),
):
    auth = _check_auth(request)
    if auth is not None: return auth
    try:
        with get_conn() as conn:
            update_city_runtime_settings(
                conn=conn,
                city_code=city_code,
                city_name=city_name.strip(),
                timezone_name=timezone_name.strip(),
                service_day_rollover_hour=service_day_rollover_hour,
                is_enabled=is_enabled == "true",
                sim_enabled=sim_enabled == "true",
                sim_force_date=sim_force_date.strip(),
                sim_force_time=sim_force_time.strip(),
                gtfs_url=gtfs_url.strip(),
                gtfs_refresh_interval_hours=gtfs_refresh_interval_hours,
                gtfs_url_encrypted=gtfs_url_encrypted == "true",
                runtime_overrides={
                    "sim_interval_seconds": sim_interval_seconds.strip(),
                    "sim_active_trip_lookbehind_seconds": sim_active_trip_lookbehind_seconds.strip(),
                    "sim_active_trip_lookahead_seconds": sim_active_trip_lookahead_seconds.strip(),
                    "sim_apply_trip_updates": sim_apply_trip_updates.strip(),
                    "fusion_interval_seconds": fusion_interval_seconds.strip(),
                    "fusion_rt_max_age_seconds": fusion_rt_max_age_seconds.strip(),
                    "fusion_min_match_score": fusion_min_match_score.strip(),
                    "fusion_max_shape_distance_m": fusion_max_shape_distance_m.strip(),
                    "stop_arrivals_interval_seconds": stop_arrivals_interval_seconds.strip(),
                    "stop_arrivals_horizon_seconds": stop_arrivals_horizon_seconds.strip(),
                    "stop_arrivals_limit_per_stop": stop_arrivals_limit_per_stop.strip(),
                    "stop_arrivals_shape_speed_mps": stop_arrivals_shape_speed_mps.strip(),
                    "stop_arrivals_shape_max_distance_m": stop_arrivals_shape_max_distance_m.strip(),
                    "stop_arrivals_shape_passed_grace_m": stop_arrivals_shape_passed_grace_m.strip(),
                },
                source_updates={
                    "vehicle_positions": {
                        "source_value": vehicle_positions_source_value.strip(),
                        "is_enabled": vehicle_positions_enabled == "true",
                        "refresh_interval_seconds": vehicle_positions_refresh_interval_seconds,
                        "encrypt_url": bool(vehicle_positions_encrypt_url_edit),
                    },
                    "trip_updates": {
                        "source_value": trip_updates_source_value.strip(),
                        "is_enabled": trip_updates_enabled == "true",
                        "refresh_interval_seconds": trip_updates_refresh_interval_seconds,
                        "encrypt_url": bool(trip_updates_encrypt_url_edit),
                    },
                    "alerts": {
                        "source_value": alerts_source_value.strip(),
                        "is_enabled": alerts_enabled == "true",
                        "refresh_interval_seconds": alerts_refresh_interval_seconds,
                        "encrypt_url": bool(alerts_encrypt_url_edit),
                    },
                },
            )
            conn.commit()
            city = get_city_detail(conn, city_code)
        return render_edit_page(city, message="City settings saved.", is_error=False)
    except Exception as e:
        with get_conn() as conn:
            city = get_city_detail(conn, city_code)
        if city is None:
            raise HTTPException(status_code=404, detail="City not found")
        return render_edit_page(city, message=str(e), is_error=True)


