import os
import uuid
import json
import sqlite3
import time
import re
import requests
import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go

# ==========================================
# 1. АНТИФРОД И ПРОКТОРИНГ (С ПОДСЧЕТОМ)
# ==========================================

def inject_proctoring_js():
    """JS для блокировки действий и подсчета уходов с вкладки с передачей в URL."""
    js_code = """
    <script>
    // Читаем текущее количество нарушений из URL или ставим 0
    let cheatCount = parseInt(new URL(window.parent.location.href).searchParams.get('cheat_count') || '0');

    const blockCopyPaste = () => {
        const inputs = window.parent.document.querySelectorAll('textarea, input');
        inputs.forEach(input => {
            input.onpaste = (e) => { e.preventDefault(); alert('ПРОКТОРИНГ: Вставка текста запрещена!'); return false; };
            input.oncopy = (e) => e.preventDefault();
            input.oncontextmenu = (e) => e.preventDefault();
        });
    }
    setInterval(blockCopyPaste, 1000);

    // Детектор переключения вкладок
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === 'hidden') {
            cheatCount++;
            alert('ПРОКТОРИНГ: Зафиксировано переключение вкладки! Нарушение записано в отчет.');
            
            // "Мост" передачи данных: записываем счетчик в URL параметры для Python
            const url = new URL(window.parent.location.href);
            url.searchParams.set('cheat_count', cheatCount);
            window.parent.history.pushState({}, '', url);
        }
    });
    </script>
    """
    components.html(js_code, height=0)


# ==========================================
# 2. БАЗА ДАННЫХ И ВИЗУАЛИЗАЦИЯ
# ==========================================

def init_db():
    conn = sqlite3.connect('hr_adaptive_platform.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS adaptive_reports (
            id TEXT PRIMARY KEY,
            role_type TEXT,
            target_pos TEXT,
            dialog_history TEXT,
            analysis_text TEXT,
            radar_data TEXT,
            cheat_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_report(role, pos, history, analysis, radar_data, cheat_count):
    report_id = str(uuid.uuid4())
    conn = sqlite3.connect('hr_adaptive_platform.db')
    c = conn.cursor()
    c.execute(
        "INSERT INTO adaptive_reports (id, role_type, target_pos, dialog_history, analysis_text, radar_data, cheat_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (report_id, role, pos, json.dumps(history, ensure_ascii=False), analysis,
         json.dumps(radar_data, ensure_ascii=False), cheat_count))
    conn.commit()
    conn.close()
    return report_id

def get_report(report_id):
    conn = sqlite3.connect('hr_adaptive_platform.db')
    c = conn.cursor()
    c.execute("SELECT role_type, target_pos, dialog_history, analysis_text, radar_data, cheat_count FROM adaptive_reports WHERE id=?", (report_id,))
    res = c.fetchone()
    conn.close()
    return res

def draw_gauge_chart(score):
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score,
        title={'text': "Интегральный индекс компетенций", 'font': {'size': 18}},
        gauge={'axis': {'range': [0, 10]}, 'bar': {'color': "#2E86C1"}}
    ))
    fig.update_layout(height=250, margin=dict(l=20, r=20, t=40, b=20))
    st.plotly_chart(fig, use_container_width=True)

def draw_radar_chart(data_dict):
    categories = list(data_dict.keys())
    values = list(data_dict.values())
    categories.append(categories[0])
    values.append(values[0])
    fig = go.Figure(data=go.Scatterpolar(r=values, theta=categories, fill='toself', line_color='#2E86C1'))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 10])), height=300, margin=dict(l=40, r=40, t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)


# ==========================================
# 3. ЛОГИКА GIGACHAT (IRT & HIGH-PRECISION)
# ==========================================

class GigaChatIntegration:
    def __init__(self, auth_key):
        self.auth_key = auth_key
        self.token = self._get_token()
        self.url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def _get_token(self):
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'RqUID': str(uuid.uuid4()), 'Authorization': f'Basic {self.auth_key}'}
        try:
            res = requests.post(url, headers=headers, data={'scope': 'GIGACHAT_API_PERS'}, verify=False)
            return res.json().get('access_token')
        except: return None

    def ask(self, system_prompt, history):
        if not self.token: return "Ошибка авторизации."
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        payload = {"model": "GigaChat", "messages": [{"role": "system", "content": system_prompt}] + history, "temperature": 0.6}
        res = requests.post(self.url, headers=headers, json=payload, verify=False)
        return res.json()['choices'][0]['message']['content'] if res.status_code == 200 else "Ошибка API."

def get_adaptive_question_prompt(role, pos, step, max_steps):
    if role == "Соискатель":
        return f"""Ты — ведущий технический эксперт в области {pos}. Твоя цель — за {max_steps} вопроса выявить "фейкового" кандидата.
        ШАГ: {step}/{max_steps}.
        ЗАДАЧА: Сформулируй ОДИН предельно конкретный вопрос по стеку {pos}.
        ТРЕБОВАНИЯ:
        1. Ответ должен занимать НЕ БОЛЕЕ 1-2 предложений.
        2. Никакой теории (без "Что такое..."). Используй: "Представь ситуацию...", "Что будет, если...", "Назови ключевое отличие в контексте...".
        3. Если ответ верный — усложняй до Senior/Architect. Если слабый — бей в фундаментальные основы.
        ПРАВИЛО: Выводи ТОЛЬКО текст вопроса."""
    else:
        scenario = ["Знакомство и ответственность.", "Current State (энергия и драйв).", "Competencies (кейс STAR).", "Weaknesses (что делегировать?).", "Future (амбиции).", "Deep Dive."]
        curr_task = scenario[min(step-1, len(scenario)-1)]
        return f"""Ты — Старший HR-бизнес-партнер и коуч. Сотрудник: {pos}. Шаг: {step}/{max_steps}.
        ЭТАП: {curr_task}. Веди диалог эмпатично. Задавай ПО ОДНОМУ вопросу. Выводи только текст вопроса."""

def get_final_analysis_prompt(role, pos, transcript, cheat_count):
    base_rules = "Стиль: профессиональный HR-аудит. Запрещено использовать эмодзи."
    
    # Добавляем данные о прокторинге в мозг ИИ
    proctoring_data = f"\nВНИМАНИЕ: Система прокторинга зафиксировала, что кандидат переключал вкладки браузера {cheat_count} раз во время ответа на вопросы."
    if cheat_count > 0:
        proctoring_data += " ЭТО ПРЯМОЕ ДОКАЗАТЕЛЬСТВО ПОИСКА ОТВЕТОВ В ИНТЕРНЕТЕ. Обязательно жестко отрази это в рисках и вердикте!"
    
    if role == "Соискатель":
        return f"""Ты — HR-директор. Проведи аудит экспресс-интервью на позицию {pos}.
        [СТЕНОГРАММА] {transcript} [/КОНЕЦ СТЕНОГРАММЫ]
        {proctoring_data}
        
        ЗАДАЧИ: 1. Оценить "Профессиональный Рефлекс" (лаконичность и точность). 2. Детекция Фрода (поиск ответов, шаблонность, ИИ-стиль). 3. Сильные/слабые стороны.
        ФОРМАТ: Резюме компетенций, Сильные стороны, Риски (включая данные о переключении вкладок), Вердикт.
        В конце JSON:
        ```json
        {{"Техническая_Точность": 0, "Скорость_Мышления": 0, "Практический_Опыт": 0, "Лаконичность": 0, "Устойчивость_к_проверке": 0}}
        ```
        {base_rules}"""
    else:
        return f"""Ты — HR-директор. Проанализируй интервью развития ({pos}).
        [СТЕНОГРАММА] {transcript} [/КОНЕЦ СТЕНОГРАММЫ]
        СОСТАВЬ: Профиль, Психологический статус (выгорание), Матрица компетенций, Точки роста, Карьерный трек, Action Plan.
        В конце JSON:
        ```json
        {{"Проактивность": 0, "Бизнес_Видение": 0, "Стрессоустойчивость": 0, "Мотивация": 0, "Самостоятельность": 0}}
        ```
        {base_rules}"""


# ==========================================
# 4. ИНТЕРФЕЙС
# ==========================================

def main():
    st.set_page_config(page_title="Modular HR-Tech System", layout="centered")
    init_db()
    AUTH_KEY = st.secrets.get("GIGACHAT_KEY", "")
    giga = GigaChatIntegration(AUTH_KEY)

    if "report" in st.query_params:
        show_hr_view(st.query_params["report"])
        return

    inject_proctoring_js()
    st.title("Модульная система оценки персонала")

    if 'step' not in st.session_state:
        st.session_state.update({'step': "role_selection", 'messages': [], 'q_count': 0})

    if st.session_state.step == "role_selection":
        c1, c2 = st.columns(2)
        if c1.button("Наём (Micro-Assessment)", use_container_width=True):
            st.session_state.update({'role': "Соискатель", 'max_q': 6, 'step': "pos_input"})
            st.rerun()
        if c2.button("Развитие (Career Coach)", use_container_width=True):
            st.session_state.update({'role': "Сотрудник", 'max_q': 6, 'step': "pos_input"})
            st.rerun()

    elif st.session_state.step == "pos_input":
        pos = st.text_input("Укажите позицию/стек:")
        if st.button("Начать") and pos.strip():
            st.session_state.update({'pos': pos, 'step': "interview", 'q_count': 1})
            # Сбрасываем URL параметр при новом интервью
            st.query_params.clear()
            with st.spinner("Генерация первого вопроса..."):
                q = giga.ask(get_adaptive_question_prompt(st.session_state.role, pos, 1, st.session_state.max_q), [])
                st.session_state.messages.append({"role": "assistant", "content": q})
            st.rerun()

    elif st.session_state.step == "interview":
        for m in st.session_state.messages:
            with st.chat_message(m["role"]): st.write(m["content"])
        
        user_input = st.chat_input("Ваш лаконичный ответ...")
        if user_input:
            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state.q_count += 1
            if st.session_state.q_count <= st.session_state.max_q:
                with st.spinner("Анализ..."):
                    hist = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    q = giga.ask(get_adaptive_question_prompt(st.session_state.role, st.session_state.pos, st.session_state.q_count, st.session_state.max_q), hist)
                    st.session_state.messages.append({"role": "assistant", "content": q})
                st.rerun()
            else:
                st.session_state.step = "analysis"
                st.rerun()

    elif st.session_state.step == "analysis":
        with st.spinner("Финальный аудит и проверка метрик прокторинга..."):
            # Считываем счетчик фрода из URL, который туда положил JavaScript
            cheat_count = int(st.query_params.get("cheat_count", 0))
            
            transcript = "".join([f"{'ИИ' if m['role']=='assistant' else 'Кандидат'}: {m['content']}\n" for m in st.session_state.messages])
            
            # Передаем счетчик в нейросеть
            raw = giga.ask(get_final_analysis_prompt(st.session_state.role, st.session_state.pos, transcript, cheat_count), [])
            
            radar_data = {}
            json_match = re.search(r'```json\n(.*?)\n```', raw, re.DOTALL)
            if json_match:
                try:
                    radar_data = json.loads(json_match.group(1))
                    text_report = raw.replace(json_match.group(0), "").strip()
                except:
                    text_report = raw
            else: text_report = raw

            # Сохраняем в базу данных
            rid = save_report(st.session_state.role, st.session_state.pos, st.session_state.messages, text_report, radar_data, cheat_count)
            
            st.success("Интервью завершено.")
            st.code(f"https://your-app.streamlit.app/?report={rid}")
            if st.button("На главную"):
                st.query_params.clear()
                for k in list(st.session_state.keys()): del st.session_state[k]
                st.rerun()

def show_hr_view(report_id):
    st.title("HR-Аналитика и Прокторинг")
    expected = st.secrets.get("HR_PIN")
    if 'hr_auth' not in st.session_state: st.session_state.hr_auth = False
    
    if not st.session_state.hr_auth:
        pin = st.text_input("Введите PIN-код:", type="password")
        if st.button("Войти") and pin == expected:
            st.session_state.hr_auth = True
            st.rerun()
        return

    data = get_report(report_id)
    if data:
        role, pos, hist_j, analysis, radar_j, cheat_count = data
        st.markdown(f"### {role}: {pos}")
        
        # --- ВЫВОД МЕТРИК ПРОКТОРИНГА ---
        st.divider()
        if role == "Соискатель":
            st.markdown("#### Аппаратный Антифрод Контроль")
            m1, m2 = st.columns(2)
            if cheat_count > 0:
                m1.metric(label="Потеря фокуса (переключение вкладок)", value=f"{cheat_count} раз", delta="🚨 Высокий риск списывания", delta_color="inverse")
            else:
                m1.metric(label="Потеря фокуса (переключение вкладок)", value="0 раз", delta="✅ Нарушений не выявлено", delta_color="normal")
            
            m2.metric(label="Попытки вставки текста (Ctrl+V)", value="Заблокировано JS")
        st.divider()
        
        radar_data = json.loads(radar_j)
        if radar_data:
            c1, c2 = st.columns(2)
            with c1: draw_gauge_chart(sum(radar_data.values())/len(radar_data))
            with c2: draw_radar_chart(radar_data)
        st.markdown(analysis)
        
        messages = json.loads(hist_j)
        transcript = "\n".join([f"{'Система' if m['role']=='assistant' else 'Кандидат'}: {m['content']}" for m in messages])
        
        # Обновляем текст выгружаемого файла
        fraud_text = f"ТЕХНИЧЕСКИЕ НАРУШЕНИЯ: Переключений вкладок: {cheat_count}\n\n" if role == "Соискатель" else ""
        st.download_button("Скачать отчет", f"ОТЧЕТ: {pos}\n\n{fraud_text}{analysis}\n\nСТЕНОГРАММА:\n{transcript}")

if __name__ == "__main__":
    main()
