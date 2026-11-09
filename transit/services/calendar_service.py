from datetime import date, datetime, timedelta


def get_service_date_and_time(
    now: datetime,
    forced_date: str | None,
    forced_time: str | None,
    rollover_hour: int,
) -> tuple[date, str]:
    if forced_date:
        service_date = datetime.strptime(forced_date, "%Y-%m-%d").date()

        if forced_time:
            return service_date, forced_time

        if now.hour < rollover_hour:
            service_seconds = (now.hour + 24) * 3600 + now.minute * 60 + now.second
        else:
            service_seconds = now.hour * 3600 + now.minute * 60 + now.second

        h = service_seconds // 3600
        rem = service_seconds % 3600
        m = rem // 60
        s = rem % 60
        return service_date, f"{h:02d}:{m:02d}:{s:02d}"

    if now.hour < rollover_hour:
        service_date = now.date() - timedelta(days=1)
        service_seconds = (now.hour + 24) * 3600 + now.minute * 60 + now.second
    else:
        service_date = now.date()
        service_seconds = now.hour * 3600 + now.minute * 60 + now.second

    h = service_seconds // 3600
    rem = service_seconds % 3600
    m = rem // 60
    s = rem % 60
    return service_date, f"{h:02d}:{m:02d}:{s:02d}"
