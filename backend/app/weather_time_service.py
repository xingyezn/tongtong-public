"""Server-side weather and time tools for the conversational model."""

import datetime as dt
import json
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from lunar_python import Solar
    from lunar_python.util import HolidayUtil
except ImportError:  # Keep ordinary time queries usable before dependencies update.
    Solar = None
    HolidayUtil = None


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "server.weather.get",
        "description": (
            "查询指定城市或地区的当前天气。必须使用用户明确提供的地点；"
            "如果地点不明确，应先向用户确认。返回温度、体感温度、天气状况、"
            "降水、湿度和风速。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "城市或地区名称，例如上海、北京市、Tokyo",
                },
            },
            "required": ["location"],
        },
    },
}

TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "server.time.now",
        "description": (
            "查询当前日期、时间、农历日期、生肖，并报告未来7天内的法定节假日、"
            "传统节日和二十四节气。默认使用中国上海时区；用户指定其他城市或"
            "时区时，传入 IANA 时区名称，例如 America/Los_Angeles。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA 时区名称，默认 Asia/Shanghai",
                },
            },
        },
    },
}

SERVER_TOOLS = [WEATHER_TOOL, TIME_TOOL]
SERVER_TOOL_PREFIXES = ("server.weather.", "server.time.")

WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "小冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴大冰雹",
}

LOCATION_ALIASES = {
    "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou",
    "深圳": "Shenzhen", "杭州": "Hangzhou", "南京": "Nanjing",
    "苏州": "Suzhou", "成都": "Chengdu", "重庆": "Chongqing",
    "武汉": "Wuhan", "西安": "Xi'an", "天津": "Tianjin",
    "厦门": "Xiamen", "青岛": "Qingdao", "郑州": "Zhengzhou",
    "长沙": "Changsha", "合肥": "Hefei", "昆明": "Kunming",
    "福州": "Fuzhou", "济南": "Jinan", "沈阳": "Shenyang",
    "大连": "Dalian", "哈尔滨": "Harbin", "南昌": "Nanchang",
    "贵阳": "Guiyang", "太原": "Taiyuan", "无锡": "Wuxi",
    "宁波": "Ningbo", "纽约": "New York", "伦敦": "London",
    "东京": "Tokyo", "巴黎": "Paris", "悉尼": "Sydney",
    "旧金山": "San Francisco", "洛杉矶": "Los Angeles",
}


class WeatherTimeService:
    """Fetch weather data and provide timezone-aware current time."""

    def __init__(self, config=None):
        settings = (config or {}).get("weather", {})
        self.default_timezone = settings.get("default_timezone", "Asia/Shanghai")
        self.timeout = int(settings.get("timeout_seconds", 12))

    async def call(self, http_session, name, arguments):
        if name == "server.time.now":
            return self.current_time(arguments)
        if name == "server.weather.get":
            return await self.weather(http_session, arguments)
        return {"error": "unknown server tool {}".format(name)}

    def current_time(self, arguments):
        timezone_name = str((arguments or {}).get("timezone") or self.default_timezone).strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            # Windows installations may not have the IANA tzdata database.
            # Keep the default China timezone usable without an extra package.
            if timezone_name == "Asia/Shanghai":
                timezone = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
            elif timezone_name in ("UTC", "Etc/UTC"):
                timezone = dt.timezone.utc
            else:
                return {"error": "invalid timezone: {}".format(timezone_name)}
        now = dt.datetime.now(timezone)
        result = {
            "timezone": timezone_name,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "datetime": now.isoformat(timespec="seconds"),
            "weekday": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[now.weekday()],
            "timestamp": int(now.timestamp()),
        }
        result.update(self._calendar_context(now))
        return result

    @staticmethod
    def _calendar_context(now):
        if Solar is None or HolidayUtil is None:
            return {"calendar_available": False, "upcoming_events": []}

        solar = Solar.fromYmd(now.year, now.month, now.day)
        lunar = solar.getLunar()
        lunar_month = lunar.getMonth()
        result = {
            "calendar_available": True,
            "lunar": {
                "year": lunar.getYear(),
                "month": abs(lunar_month),
                "day": lunar.getDay(),
                "is_leap_month": lunar_month < 0,
                "year_text": lunar.getYearInChinese(),
                "month_text": lunar.getMonthInChinese(),
                "day_text": lunar.getDayInChinese(),
                "zodiac": lunar.getYearShengXiao(),
            },
            "upcoming_events": [],
        }
        for offset in range(7):
            date = now.date() + dt.timedelta(days=offset)
            day_solar = Solar.fromYmd(date.year, date.month, date.day)
            day_lunar = day_solar.getLunar()
            event_date = date.isoformat()
            holiday = HolidayUtil.getHoliday(event_date)
            if holiday is not None:
                result["upcoming_events"].append({
                    "date": event_date,
                    "type": "workday" if holiday.isWork() else "holiday",
                    "name": holiday.getName(),
                    "target_date": holiday.getTarget(),
                })
            current_jieqi = day_lunar.getCurrentJieQi()
            if current_jieqi is not None:
                result["upcoming_events"].append({
                    "date": event_date,
                    "type": "solar_term",
                    "name": current_jieqi.getName(),
                })
            festivals = list(day_solar.getFestivals()) + list(day_lunar.getFestivals())
            for festival in festivals:
                if festival and not any(
                        item["date"] == event_date and item["name"] == festival
                        for item in result["upcoming_events"]):
                    result["upcoming_events"].append({
                        "date": event_date,
                        "type": "festival",
                        "name": festival,
                    })
        return result

    async def weather(self, http_session, arguments):
        location = str((arguments or {}).get("location") or "").strip()
        if not location or len(location) > 100:
            return {"error": "location is required and must be at most 100 characters"}
        lookup_location = LOCATION_ALIASES.get(location.rstrip("市"), location)

        timeout = http_session.timeout if self.timeout <= 0 else None
        try:
            geocode_url = "https://geocoding-api.open-meteo.com/v1/search?{}".format(
                urlencode({"name": lookup_location, "count": 1, "language": "en", "format": "json"}))
            async with http_session.get(geocode_url, timeout=timeout or self.timeout) as response:
                geocode = await response.json(content_type=None)
                if response.status >= 400:
                    return {"error": "location lookup failed", "detail": geocode}
            results = geocode.get("results") or []
            if not results:
                return {"error": "location not found", "location": location}
            place = results[0]

            forecast_params = {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": ",".join((
                    "temperature_2m", "relative_humidity_2m", "apparent_temperature",
                    "weather_code", "precipitation", "wind_speed_10m")),
                "timezone": "auto",
                "forecast_days": 1,
            }
            forecast_url = "https://api.open-meteo.com/v1/forecast?{}".format(
                urlencode(forecast_params))
            async with http_session.get(forecast_url, timeout=timeout or self.timeout) as response:
                forecast = await response.json(content_type=None)
                if response.status >= 400:
                    return {"error": "weather lookup failed", "detail": forecast}
        except Exception as exc:
            return {"error": "weather service unavailable", "detail": str(exc)}

        current = forecast.get("current") or {}
        units = forecast.get("current_units") or {}
        code = current.get("weather_code")
        return {
            "location": {
                "name": place.get("name"),
                "admin1": place.get("admin1"),
                "country": place.get("country"),
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
                "timezone": forecast.get("timezone") or place.get("timezone"),
            },
            "observed_at": current.get("time"),
            "condition": WEATHER_CODES.get(code, "未知天气"),
            "weather_code": code,
            "temperature": current.get("temperature_2m"),
            "temperature_unit": units.get("temperature_2m", "°C"),
            "apparent_temperature": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "humidity_unit": units.get("relative_humidity_2m", "%"),
            "precipitation": current.get("precipitation"),
            "precipitation_unit": units.get("precipitation", "mm"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_speed_unit": units.get("wind_speed_10m", "km/h"),
        }


def encode_result(result):
    return json.dumps(result, ensure_ascii=False)
