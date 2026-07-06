import requests
import pandas as pd
from datetime import datetime, timedelta, date
import re
import time
import zipfile
from io import BytesIO
import concurrent.futures
import json
import os

# pykrx가 없으면 자동 설치 (GitHub Actions 환경 등)
try:
    from pykrx import stock as krx_stock
except ImportError:
    import subprocess, sys
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pykrx', '-q'])
    from pykrx import stock as krx_stock

# DART API 키
DART_API_KEY = '453e35fbd20c4c4f9f103b7fb7f2d14a710f24ac'

# ------------------------------------------------------------------
# 숫자 / 날짜 추출 유틸
# ------------------------------------------------------------------
def clean_text(text):
    """공백만 제거 (키워드 매칭용)"""
    return re.sub(r'\s+', '', str(text))

def extract_amount(text):
    """문자열에서 숫자만 뽑아 int로 반환 ('상기 4항의...' 같은 주석 무시)"""
    if text is None:
        return pd.NA
    t = str(text).replace(',', '')
    nums = re.findall(r'\d+', t)
    if nums:
        return int(nums[0])
    return pd.NA

def extract_percent(text):
    """문자열에서 숫자만 뽑아 float로 반환"""
    if text is None:
        return pd.NA
    t = str(text).replace(',', '')
    m = re.search(r'-?\d+(\.\d+)?', t)
    if not m:
        return pd.NA
    try:
        return float(m.group())
    except ValueError:
        return pd.NA

def extract_date(text):
    """다양한 표기에서 날짜를 뽑아 date 객체로 반환"""
    if text is None:
        return pd.NaT
    t = str(text)
    m = re.search(r'(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일', t)
    if not m:
        m = re.search(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', t)
    if not m:
        m = re.search(r'(\d{4})(\d{2})(\d{2})(?!\d)', t)
    if not m:
        return pd.NaT
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(y, mo, d)
    except ValueError:
        return pd.NaT

MAX_LABEL_LEN = 25

def is_label_row(items, keyword):
    if not items:
