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

def inject_proctoring_js():
    js_code = """
    <script>
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
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === 'hidden') {
            cheatCount++;
            alert('ПРОКТОРИНГ: Зафиксировано переключение вкладки!');
            const url = new URL(window.parent.location.href);
            url.searchParams.set('cheat_count', cheatCount);
            window.parent.history.pushState({}, '', url);
        }
    });
    </script>
    """
    components.html(js_code, height=0)

def init_db():
    conn = sqlite3.connect('hr_platform_v4.db')
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
    conn = sqlite3.connect('hr_platform_v4.db')
    c = conn.cursor()
    c.execute(
        "INSERT INTO adaptive_reports (id, role_type, target_pos, dialog_history, analysis_text, radar_data, cheat_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (report_id, role, pos, json.dumps(history, ensure_ascii=False), analysis,
         json.dumps(radar_data, ensure_ascii=False), cheat_count))
    conn.commit()
    conn.close()
    return report_id

def get_report(report_id):
    conn = sqlite3.connect('hr_platform_v4.db')
    c = conn.cursor()
    c.execute("SELECT role_type, target_pos, dialog_history, analysis_text, radar_data, cheat_count FROM adaptive_reports WHERE id=?", (report_id,))
    res = c.fetchone()
    conn.close()
    return res

def draw_gauge_chart(score):
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score,
        title={'text': "Интегральный индекс", 'font': {'size': 18}},
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
        return f"""Ты — строгий технический эксперт. Ты проводишь собеседование на позицию: {pos}.
        ВНИМАНИЕ: ПОЛЬЗОВАТЕЛЬ — ЭТО КАНДИДАТ. Его реплики — это ответы на твои вопросы на экзамене.
        ШАГ: {step}/{max_steps}.
        ТВОЯ ЗАДАЧА: Прочитай предыдущий ответ кандидата и задай ОДИН следующий практический вопрос.
        КРИТИЧЕСКИЕ ПРАВИЛА:
        ВЫВЕДИ ТОЛЬКО САМ ВОПРОС. Ни единого слова больше! Запрещена любая обратная связь и оценка.
        ОБЕСПЕЧЬ РАЗНОСТОРОННОСТЬ: Если кандидат ответил слабо или не по делу, кардинально смени техническую подобласть для следующего вопроса. Не зацикливайся на одной теме. Проверяй разные аспекты профессии.
        Формат вопроса: "Представь ситуацию...", "Что будет, если...". Без базовой теории."""
    else:
        scenario = ["Знакомство и ответственность.", "Current State (энергия).", "Competencies (STAR).", "Weaknesses.", "Future.", "Deep Dive."]
        curr_task = scenario[min(step-1, len(scenario)-1)]
        return f"""Ты — HR-коуч. Сотрудник: {pos}. Шаг: {step}/{max_steps}. Этап: {curr_task}.
        ВНИМАНИЕ: ПОЛЬЗОВАТЕЛЬ — ЭТО СОТРУДНИК.
        КРИТИЧЕСКОЕ ПРАВИЛО: Выведи ТОЛЬКО один вопрос. Без вступлений, без оценки его прошлых слов."""

def get_final_analysis_prompt(role, pos, transcript, cheat_count):
    base_rules = "Стиль: профессиональный HR-аудит. Запрещено использовать эмодзи."
    proctoring = f" ВНИМАНИЕ: Кандидат переключал вкладки {cheat_count} раз! Это признак списывания. Отрази это в рисках." if cheat_count > 0 else ""
    
    if role == "Соискатель":
        return f"""Ты — HR-директор. Проведи аудит интервью на позицию {pos}.
        [СТЕНОГРАММА] {transcript} [/КОНЕЦ СТЕНОГРАММЫ] {proctoring}
        ФОРМАТ: Резюме компетенций, Сильные стороны, Риски (с учетом прокторинга), Вердикт.
        В конце JSON:
        ```json
        {{"Техническая_Точность": 0, "Скорость_Мышления": 0, "Практический_Опыт": 0, "Лаконичность": 0, "Устойчивость_к_проверке": 0}}
        ```
        {base_rules}"""
    else:
        return f"""Ты — HR-директор. Проанализируй интервью развития ({pos}).
        [СТЕНОГРАММА] {transcript} [/КОНЕЦ СТЕНОГРАММЫ]
        СОСТАВЬ: Профиль, Психологический статус, Матрица компетенций, Точки роста, Карьерный трек, Action Plan.
        В конце JSON:
        ```json
        {{"Проактивность": 0, "Бизнес_Видение": 0, "Стрессоустойчивость": 0, "Мотивация": 0, "Самостоятельность": 0}}
        ```
        {base_rules}"""

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
        if c1.button("Соискатель", use_container_width=True):
            st.session_state.update({'role': "Соискатель", 'max_q': 6, 'step': "pos_input"})
            st.rerun()
        if c2.button("Сотрудник", use_container_width=True):
            st.session_state.update({'role': "Сотрудник", 'max_q': 6, 'step': "pos_input"})
            st.rerun()

    elif st.session_state.step == "pos_input":
        pos = st.text_input("Укажите позицию/стек:")
        if st.button("Начать") and pos.strip():
            st.session_state.update({'pos': pos, 'step': "interview", 'q_count': 1})
            st.query_params.clear()
            with st.spinner("Генерация первого вопроса..."):
                q = giga.ask(get_adaptive_question_prompt(st.session_state.role, pos, 1, st.session_state.max_q), [])
                st.session_state.messages.append({"role": "assistant", "content": q})
                st.session_state.start_time = time.time()
            st.rerun()

    elif st.session_state.step == "interview":
        time_limit = 90 if st.session_state.role == "Соискатель" else 120
        elapsed = time.time() - st.session_state.start_time
        remaining = max(0, time_limit - int(elapsed))

        for m in st.session_state.messages:
            with st.chat_message(m["role"]): st.write(m["content"])
        
        st.progress(remaining / time_limit)
        st.caption(f"Вопрос {st.session_state.q_count} из {st.session_state.max_q} | Осталось времени: {remaining} сек.")

        user_input = st.chat_input("Ваш лаконичный ответ...")
        
        if remaining <= 0 and not user_input:
            user_input = "[ПРОКТОРИНГ: Кандидат не уложился в отведенное время]"

        if user_input:
            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state.q_count += 1
            if st.session_state.q_count <= st.session_state.max_q:
                with st.spinner("Анализ..."):
                    hist = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    q = giga.ask(get_adaptive_question_prompt(st.session_state.role, st.session_state.pos, st.session_state.q_count, st.session_state.max_q), hist)
                    st.session_state.messages.append({"role": "assistant", "content": q})
                    st.session_state.start_time = time.time() 
                st.rerun()
            else:
                st.session_state.step = "analysis"
                st.rerun()

        time.sleep(1)
        st.rerun()

    elif st.session_state.step == "analysis":
        with st.spinner("Финальный аудит..."):
            cheat_count = int(st.query_params.get("cheat_count", 0))
            transcript = "".join([f"{'ИИ' if m['role']=='assistant' else 'Кандидат'}: {m['content']}\n" for m in st.session_state.messages])
            raw = giga.ask(get_final_analysis_prompt(st.session_state.role, st.session_state.pos, transcript, cheat_count), [])
            
            radar_data = {}
            json_match = re.search(r'
http://googleusercontent.com/immersive_entry_chip/0

Архитектура отполирована до блеска. Готов ли ты теперь зафиксировать актуальность нашего исследования и прописать четкий научный аппарат для текста Введения?
