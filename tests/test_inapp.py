"""static/inapp.js detectInApp, run in Node (skipped where Node is not installed)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = (Path(__file__).parent.parent / "static" / "inapp.js").as_uri()
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Mobile/15E148")
ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Version/4.0 Chrome/126.0.0.0 Mobile Safari/537.36")

IN_APP = [
    ("wechat", f"{IPHONE} MicroMessenger/8.0.49(0x18003127) NetType/WIFI Language/zh_CN", "微信", "zh"),
    ("xiaohongshu", f"{IPHONE} xhsdiscover/8.40 (iPhone16,1;iOS 17.5)", "小红书", "zh"),
    ("weibo", f"{IPHONE} Weibo (iPhone15,2__weibo__14.5.1__iphone__os17.5)", "微博", "zh"),
    ("qq", f"{IPHONE} QQ/9.0.60.611 V1_IPH_SQ_9.0.60_1_APP_A Pixel/1170", "QQ", "zh"),
    ("instagram", f"{IPHONE} Instagram 337.0.3.23.54 (iPhone15,2; iOS 17_5; en_US)", "Instagram", "en"),
    ("facebook_ios", f"{IPHONE} [FBAN/FBIOS;FBAV/470.0.0.40.109;FBDV/iPhone15,2]", "Facebook", "en"),
    ("facebook_android", f"{ANDROID} [FB_IAB/FB4A;FBAV/470.0.0.50.91;]", "Facebook", "en"),
    ("tiktok_musically", f"{IPHONE} musical_ly_34.5.0 JsSdk/2.0", "TikTok", "en"),
    ("tiktok_bytedance", f"{ANDROID} BytedanceWebview/d8a21c6", "TikTok", "en"),
    ("tiktok_name", f"{ANDROID} TikTok 34.5.3", "TikTok", "en"),
    ("snapchat", f"{IPHONE} Snapchat/12.90.0.45 (like Safari/8617.2.4)", "Snapchat", "en"),
    ("linkedin", f"{IPHONE} [LinkedInApp]/9.29.3", "LinkedIn", "en"),
    ("line", f"{IPHONE} Safari Line/14.8.0", "Line", "en"),
]

NORMAL = [
    ("iphone_safari", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"),
    ("android_chrome", "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"),
    ("desktop_chrome", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("qq_browser", "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/116.0 MQQBrowser/14.9 Mobile Safari/537.36"),
    ("empty", ""),
]


@pytest.fixture(scope="module")
def detect():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    uas = [case[1] for case in IN_APP + NORMAL]
    script = (f"const {{detectInApp}} = await import({json.dumps(MODULE)});"
              "const uas = JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(uas.map(detectInApp)));")
    done = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(uas)],
                          capture_output=True, text=True, timeout=30, check=True)
    return dict(zip(uas, json.loads(done.stdout)))


@pytest.mark.parametrize("ua,name,lang", [c[1:] for c in IN_APP], ids=[c[0] for c in IN_APP])
def test_in_app_browser_is_detected(detect, ua, name, lang):
    assert detect[ua] == {"name": name, "lang": lang}


@pytest.mark.parametrize("ua", [c[1] for c in NORMAL], ids=[c[0] for c in NORMAL])
def test_normal_browser_is_not_flagged(detect, ua):
    assert detect[ua] is None
