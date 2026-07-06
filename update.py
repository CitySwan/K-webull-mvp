import requests
import pandas as pd
from datetime import datetime, timedelta, date
import re
import time
import zipfile
from io import BytesIO
import concurrent.futures

# pykrx가 없으면 자동 설치 (KRX 주가 조회용)
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
    """
    '550원', '5,000', '1,234.5' 같은 문자열에서 숫자만 뽑아 int/float로 반환.
    못 찾으면 pandas.NA
    """
    if text is None:
        return pd.NA
    t = str(text).replace(',', '')
    m = re.search(r'-?\d+(\.\d+)?', t)
    if not m:
        return pd.NA
    num = m.group()
    try:
        if '.' in num:
            return float(num)
        return int(num)
    except ValueError:
        return pd.NA


def extract_percent(text):
    """'2.5%', '2.5' 등에서 숫자만 뽑아 float로 반환"""
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
    """
    '2026년07월21일', '2026.07.21', '2026-07-21', '20260721' 등 다양한 표기에서
    날짜를 뽑아 date 객체로 반환. 못 찾으면 pandas.NaT
    """
    if text is None:
        return pd.NaT
    t = str(text)

    # 2026년 07월 21일 / 2026년7월21일
    m = re.search(r'(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일', t)
    if not m:
        # 2026-07-21 / 2026.07.21 / 2026/07/21
        m = re.search(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', t)
    if not m:
        # 20260721 (구분자 없는 8자리)
        m = re.search(r'(\d{4})(\d{2})(\d{2})(?!\d)', t)

    if not m:
        return pd.NaT

    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(y, mo, d)
    except ValueError:
        return pd.NaT


# 라벨 셀로 인정할 최대 길이(이보다 길면 각주 문단으로 간주하고 무시)
MAX_LABEL_LEN = 25


def is_label_row(items, keyword):
    """items[0]이 keyword를 포함하면서, 각주가 아닌 '진짜 라벨'인지 판정"""
    if not items:
        return False
    label = items[0]
    return keyword in label and len(label) <= MAX_LABEL_LEN


def pick_value(items, prefer_keyword=None):
    """
    라벨을 제외한 나머지 셀들(items[1:]) 중에서 값으로 쓸 셀 하나를 고른다.
    prefer_keyword(예: '보통주식')가 있으면 그 다음 셀을 우선 사용.
    """
    rest = items[1:]
    if not rest:
        return None
    if prefer_keyword and prefer_keyword in items:
        idx = items.index(prefer_keyword)
        if idx + 1 < len(items):
            return items[idx + 1]
    return rest[-1]


# ------------------------------------------------------------------
# 공시 원문 파싱
# ------------------------------------------------------------------
def parse_dividend_details_api(row):
    """공식 Open DART API '공시원문 다운로드' 기능을 활용해 배당 상세 항목을 추출"""
    corp_name = row['corp_name']
    rcept_no = row['rcept_no']
    stock_code = row.get('stock_code', '') if hasattr(row, 'get') else ''

    base_info = {
        '회사명': corp_name,
        '종목코드': stock_code if stock_code else pd.NA,
        '공시일자': row['rcept_dt'],
        '배당구분': '-',
        '1주당 배당금(원)': pd.NA,   # 숫자 (int)
        '시가배당율(%)': pd.NA,      # 회사가 공시문에 직접 기재한 값 (float)
        '공시시점주가(원)': pd.NA,   # 공시일 직전 거래일 종가 (int)
        '계산배당율(%)': pd.NA,      # 배당금/주가로 직접 계산한 값 (float)
        '배당기준일': pd.NaT,        # 날짜 (date)
        '지급예정일': pd.NaT,        # 날짜 (date)
        '공시링크': f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
    }

    doc_url = "https://opendart.fss.or.kr/api/document.xml"
    params = {'crtfc_key': DART_API_KEY, 'rcept_no': rcept_no}

    try:
        res = requests.get(doc_url, params=params, timeout=10)

        with zipfile.ZipFile(BytesIO(res.content)) as z:
            xml_files = [f for f in z.namelist() if f.endswith('.xml')]
            if not xml_files:
                return base_info
            xml_data = z.read(xml_files[0]).decode('utf-8', errors='ignore')

        try:
            tables = pd.read_html(xml_data)
        except ValueError:
            return base_info

        for tbl in tables:
            try:
                tbl_str = tbl.astype(str).map(clean_text)
            except AttributeError:
                tbl_str = tbl.astype(str).applymap(clean_text)

            # 핵심 키워드가 있는 '배당 내역 표'인지 확인
            if not tbl_str.apply(
                lambda x: x.str.contains('배당구분|시가배당율|배당기준일|1주당배당금')
            ).any().any():
                continue

            for idx, r in tbl.iterrows():
                items = [clean_text(i) for i in r.dropna().astype(str).tolist()]
                if not items:
                    continue

                if is_label_row(items, '배당구분'):
                    val = pick_value(items)
                    if val:
                        base_info['배당구분'] = val

                elif is_label_row(items, '1주당배당금'):
                    val = pick_value(items, prefer_keyword='보통주식')
                    amount = extract_amount(val)
                    if pd.notna(amount):
                        base_info['1주당 배당금(원)'] = amount

                elif is_label_row(items, '시가배당율'):
                    val = pick_value(items, prefer_keyword='보통주식')
                    pct = extract_percent(val)
                    if pd.notna(pct):
                        base_info['시가배당율(%)'] = pct

                elif is_label_row(items, '배당기준일'):
                    val = pick_value(items)
                    dt = extract_date(val)
                    if pd.notna(dt):
                        base_info['배당기준일'] = dt

                elif is_label_row(items, '배당금지급') or is_label_row(items, '지급예정일'):
                    val = pick_value(items)
                    dt = extract_date(val)
                    if pd.notna(dt):
                        base_info['지급예정일'] = dt

            # 유효한 데이터를 하나라도 찾았으면 표 탐색 종료
            if base_info['배당구분'] != '-' or pd.notna(base_info['1주당 배당금(원)']):
                break

        return base_info

    except zipfile.BadZipFile:
        base_info['배당구분'] = 'API 인증 오류'
        return base_info
    except Exception:
        base_info['배당구분'] = '표 양식 다름'
        return base_info


# ------------------------------------------------------------------
# 공시시점 주가 조회 (KRX) 및 배당율 계산
# ------------------------------------------------------------------
def get_price_before_disclosure(stock_code, rcept_dt, max_lookback_days=10):
    """
    공시일(rcept_dt, 'YYYYMMDD') 직전 거래일의 종가를 pykrx로 조회.
    주말/공휴일 등으로 거래가 없던 날은 하루씩 더 거슬러 올라가며 탐색.
    """
    if stock_code is None or pd.isna(stock_code) or str(stock_code).strip() == '':
        return pd.NA

    ticker = str(stock_code).zfill(6)

    try:
        base_dt = datetime.strptime(str(rcept_dt), '%Y%m%d')
    except ValueError:
        return pd.NA

    for i in range(1, max_lookback_days + 1):
        check_dt = (base_dt - timedelta(days=i)).strftime('%Y%m%d')
        try:
            df_price = krx_stock.get_market_ohlcv_by_date(check_dt, check_dt, ticker)
        except Exception:
            continue
        if df_price is not None and not df_price.empty and '종가' in df_price.columns:
            close_price = df_price['종가'].iloc[0]
            if close_price and close_price > 0:
                return int(close_price)

    return pd.NA


def enrich_with_price_and_yield(df):
    """공시시점주가(원), 계산배당율(%) 컬럼을 채워 넣는다."""
    prices = []
    for _, r in df.iterrows():
        price = get_price_before_disclosure(r.get('종목코드'), r.get('공시일자'))
        prices.append(price)
        time.sleep(0.15)  # KRX 서버 과도한 요청 방지

    df['공시시점주가(원)'] = prices

    def calc_yield(row):
        amount = row['1주당 배당금(원)']
        price = row['공시시점주가(원)']
        if pd.isna(amount) or pd.isna(price) or price == 0:
            return pd.NA
        return round(float(amount) / float(price) * 100, 2)

    df['계산배당율(%)'] = df.apply(calc_yield, axis=1)
    return df


# ------------------------------------------------------------------
# 공시 목록 수집 및 실행
# ------------------------------------------------------------------
def get_advanced_dividend_alerts(days=7):
    end_date = datetime.today().strftime('%Y%m%d')
    start_date = (datetime.today() - timedelta(days=days)).strftime('%Y%m%d')
    url = "https://opendart.fss.or.kr/api/list.json"

    all_filings = []
    page_no = 1
    total_page = 1

    print(f"📡 1단계: 최근 {days}일({start_date} ~ {end_date}) 공시 수집 중...")

    while page_no <= total_page:
        params = {
            'crtfc_key': DART_API_KEY, 'bgn_de': start_date, 'end_de': end_date,
            'page_no': page_no, 'page_count': 100
        }
        response = requests.get(url, params=params)
        data = response.json()

        if data.get('status') == '000':
            total_page = data.get('total_page', 1)
            all_filings.extend(data['list'])
            page_no += 1
        else:
            break

    df = pd.DataFrame(all_filings)
    if df.empty:
        return pd.DataFrame()

    dividend_df = df[df['report_nm'].str.contains('현금ㆍ현물배당결정')].copy()
    if dividend_df.empty:
        return pd.DataFrame()

    print(f"🕵️ 2단계: 총 {len(dividend_df)}건 발견! Open DART API로 원문을 직접 다운로드하여 분석 중입니다...")

    result_list = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(parse_dividend_details_api, row) for idx, row in dividend_df.iterrows()]
        for future in concurrent.futures.as_completed(futures):
            result_list.append(future.result())

    final_df = pd.DataFrame(result_list)
    final_df = final_df.sort_values(by='공시일자', ascending=False)

    return final_df

import json

# ================= 실행 부분 =================
df_result = get_advanced_dividend_alerts(days=7)

if not df_result.empty:
    print("💰 3단계: 공시시점 주가 조회 및 배당율 계산 중...")
    
    # 1. 숫자 포맷 지정
    df_result['1주당 배당금(원)'] = pd.to_numeric(df_result['1주당 배당금(원)'], errors='coerce')
    df_result['공시시점주가(원)'] = pd.to_numeric(df_result['공시시점주가(원)'], errors='coerce')
    
    # 2. 날짜는 문자열(YYYY-MM-DD) 형식으로 확실히 변환
    df_result['배당기준일'] = pd.to_datetime(df_result['배당기준일'], errors='coerce').dt.strftime('%Y-%m-%d')
    df_result['지급예정일'] = pd.to_datetime(df_result['지급예정일'], errors='coerce').dt.strftime('%Y-%m-%d')
    
    df_result = enrich_with_price_and_yield(df_result)
    
    # 3. 데이터프레임을 JSON 호환 딕셔너리로 변환 (NaN 값 처리)
    json_data = df_result.where(pd.notnull(df_result), None).to_dict(orient='records')
    
    # 4. data.json 파일로 저장
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    print("\n✅ data.json 생성 완료!")
else:
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump([], f)
    print("\n해당 기간 내에 배당 공시가 없습니다.")
