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
# 1. АНТИФРОД, ПРОКТОРИНГ И ЗАЩИТА СЕССИИ
# ==========================================

def inject_proctoring_js():
    """Инъекция JS для блокировки копирования и детекции переключения вкладок."""
    js_code = """
    <script>
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
            alert('ПРОКТОРИНГ: Зафиксировано переключение вкладки! Система фиксирует это в отчете.');
        }
    });

    window.addEventListener("beforeunload", (e) => {
        e.preventDefault();
        e.returnValue = 'Прогресс интервью может быть утерян.';
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_report(role, pos, history, analysis, radar_data):
    report_id = str(uuid.uuid4())
    conn = sqlite3.connect('hr_adaptive_platform.db')
    c = conn.cursor()
    c.execute(
        "INSERT INTO adaptive_reports (id, role_type, target_pos, dialog_history, analysis_text, radar_data) VALUES (?, ?, ?, ?, ?, ?)",
        (report_id, role, pos, json.dumps(history, ensure_ascii=False), analysis,
         json.dumps(radar_data, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return report_id

def get_report(report_id):
    conn = sqlite3.connect('hr_adaptive_platform.db')
    c = conn.cursor()
    c.execute(
        "SELECT role_type, target_pos, dialog_history, analysis_text, radar_data FROM adaptive_reports WHERE id=?",
        (report_id,))
    res = c.fetchone()
    conn.close()
    return res

def draw_gauge_chart(score):
    """Визуализация общего уровня компетенций (Спидометр)."""
    if score < 4:
        level, color = "Слабый", "#E74C3C"
    elif score < 8:
        level, color = "Средний", "#F39C12"
    else:
        level, color = "Сильный", "#27AE60"

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        title={'text': f"Общий уровень: {level}", 'font': {'size': 18}},
        gauge={
            'axis': {'range': [0, 10]},
            'bar': {'color': color},
            'steps': [
                {'range': [0, 4], 'color': "rgba(231, 76, 60, 0.1)"},
                {'range': [4, 8], 'color': "rgba(243, 156, 18, 0.1)"},
                {'range': [8, 10], 'color': "rgba(39, 174, 96, 0.1)"}
            ]
        }
    ))
    fig.update_layout(height=250, margin=dict(l=20, r=20, t=40, b=20))
    st.plotly_chart(fig, use_container_width=True)

def draw_radar_chart(data_dict):
    """Визуализация матрицы компетенций (Радар)."""
    categories = list(data_dict.keys())
    values = list(data_dict.values())
    categories.append(categories[0])
    values.append(values[0])

    fig = go.Figure(data=go.Scatterpolar(
        r=values, theta=categories, fill='toself',
        line_color='#2E86C1', fillcolor='rgba(46, 134, 193, 0.4)'
    ))
    fig.update_layout(
        polar=dict(radialaxis=dict(visible=True, range=[0, 10])),
        margin=dict(l=40, r=40, t=20, b=20), height=300
    )
    st.plotly_chart(fig, use_container_width=True)


# ==========================================
# 3. ИНТЕГРАЦИЯ GIGACHAT И ПРОМПТЫ
# ==========================================

class GigaChatIntegration:
    def __init__(self, auth_key):
        self.auth_key = auth_key
        self.token = self._get_token()
        self.url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def _get_token(self):
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'RqUID': str(uuid.uuid4()),
            'Authorization': f'Basic {self.auth_key}'
        }
        try:
            res = requests.post(url, headers=headers, data={'scope': 'GIGACHAT_API_PERS'}, verify=False)
            return res.json().get('access_token')
        except: return None

    def ask(self, system_prompt, history):
        if not self.token: return "Ошибка авторизации."
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "system", "content": system_prompt}] + history,
            "temperature": 0.6
        }
        res = requests.post(self.url, headers=headers, json=payload, verify=False)
        return res.json()['choices'][0]['message']['content'] if res.status_code == 200 else "Ошибка API."


def get_adaptive_question_prompt(role, pos, step, max_steps):
    """Генерация следующего вопроса в зависимости от роли."""
    if role == "Соискатель":
        return f"""Ты — строгий Технический Интервьюер. Вакансия: {pos}. Шаг: {step}/{max_steps}.
        ЗАДАЧА: Задай ОДИН короткий технический вопрос. Если прошлый ответ сильный — усложняй, если слабый — проверяй базу.
        ПРАВИЛО: Выводи ТОЛЬКО текст вопроса (одно предложение). Без приветствий, рассуждений и эмодзи."""
    
    else: # Роль: Сотрудник (Коучинговый подход)
        scenario = [
            "Знакомство: спроси текущую роль и ключевую ответственность.",
            "Current State: узнай оценку последнего квартала. Что драйвит, а что забирает энергию?",
            "Competencies (STAR): попроси пример самого сложного/успешного кейса за полгода.",
            "Weaknesses/Growth: спроси, какую ОДНУ задачу он бы делегировал навсегда?",
            "Future: узнай о карьерных амбициях на 1-2 года.",
            "Deep Dive: задай уточняющий вопрос по любой из озвученных тем роста."
        ]
        curr_task = scenario[min(step-1, len(scenario)-1)]
        return f"""Ты — Старший HR-бизнес-партнер и карьерный коуч. Сотрудник: {pos}. Шаг: {step}/{max_steps}.
        ТВОЙ ТЕКУЩИЙ ЭТАП: {curr_task}
        ПРАВИЛА: 
        1. Веди диалог эмпатично, используя активное слушание.
        2. Задавай строго ПО ОДНОМУ вопросу.
        3. Не используй эмодзи и вступительные фразы типа '🤖 ИИ:'. 
        4. Выводи только текст вопроса."""

def get_final_analysis_prompt(role, pos, transcript):
    """Генерация итогового аналитического отчета."""
    base_rules = "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать эмодзи. Стиль: строгий академический бизнес-аудит."
    
    if role == "Соискатель":
        return f"""Ты — Технический Аудитор. Проведи жесткий аудит стенограммы на позицию {pos}.
        [СТЕНОГРАММА] {transcript} [/КОНЕЦ СТЕНОГРАММЫ]
        {base_rules}
        ЕСЛИ ответы — отписки (1-3 слова) или саботаж: Выведи вердикт 'ПРОВАЛЕН (Саботаж)'.
        ИНАЧЕ: Оцени профиль компетенций и дай вердикт.
        В конце выведи JSON с оценками 0-10: 
        ```json
        {{"Hard_Skills": 0, "Когнитивная_Гибкость": 0, "Уверенность": 0, "Этика": 0, "Системное_Мышление": 0}}
        ```"""
    
    else: # Роль: Сотрудник
        return f"""Ты — Старший HR-бизнес-партнер. Проанализируй интервью сотрудника ({pos}).
        [СТЕНОГРАММА] {transcript} [/КОНЕЦ СТЕНОГРАММЫ]
        {base_rules}
        СОСТАВЬ ОТЧЕТ ПО ШАБЛОНУ:
        - Профиль сотрудника: (Должность, фокус)
        - Психологический профиль: (Энергия, вовлеченность, риски выгорания)
        - Матрица компетенций: (Развитые навыки и зоны внимания)
        - Точки роста: (Конкретные паттерны или навыки)
        - Карьерный трек: (Вертикальный/горизонтальный рост)
        - Action Plan: (3 шага на 3 месяца)
        В конце выведи JSON с оценками 0-10:
        ```json
        {{"Проактивность": 0, "Бизнес_Видение": 0, "Стрессоустойчивость": 0, "Мотивация": 0, "Самостоятельность": 0}}
        ```"""


# ==========================================
# 4. ИНТЕРФЕЙС И ЛОГИКА ПРИЛОЖЕНИЯ
# ==========================================

def main():
    st.set_page_config(page_title="HR-Tech Adaptive Platform", layout="centered")
    init_db()

    # Секреты
    AUTH_KEY = st.secrets.get("GIGACHAT_KEY", "")
    giga = GigaChatIntegration(AUTH_KEY)

    if "report" in st.query_params:
        show_hr_view(st.query_params["report"])
        return

    inject_proctoring_js()
    st.title("Система автоматизированной оценки")

    if 'step' not in st.session_state:
        st.session_state.update({'step': "role_selection", 'messages': [], 'q_count': 0})

    # ЭТАП 1: Выбор роли
    if st.session_state.step == "role_selection":
        st.subheader("Выберите тип оценки:")
        c1, c2 = st.columns(2)
        if c1.button("Наём (Соискатель)", use_container_width=True):
            st.session_state.update({'role': "Соискатель", 'max_q': 8, 'step': "pos_input"})
            st.rerun()
        if c2.button("Развитие (Сотрудник)", use_container_width=True):
            st.session_state.update({'role': "Сотрудник", 'max_q': 6, 'step': "pos_input"})
            st.rerun()

    # ЭТАП 2: Ввод должности
    elif st.session_state.step == "pos_input":
        label = "Целевая вакансия:" if st.session_state.role == "Соискатель" else "Текущая должность:"
        pos = st.text_input(label)
        if st.button("Начать интервью") and pos.strip():
            st.session_state.update({'pos': pos, 'step': "interview", 'q_count': 1, 'start_t': time.time()})
            with st.spinner("GigaChat готовит первый вопрос..."):
                q = giga.ask(get_adaptive_question_prompt(st.session_state.role, pos, 1, st.session_state.max_q), [])
                st.session_state.messages.append({"role": "assistant", "content": q})
            st.rerun()

    # ЭТАП 3: Интервью
    elif st.session_state.step == "interview":
        for m in st.session_state.messages:
            with st.chat_message(m["role"]): st.write(m["content"])

        user_input = st.chat_input("Ваш ответ...")
        if user_input:
            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state.q_count += 1
            if st.session_state.q_count <= st.session_state.max_q:
                with st.spinner("Анализ ответа..."):
                    hist = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    q = giga.ask(get_adaptive_question_prompt(st.session_state.role, st.session_state.pos, st.session_state.q_count, st.session_state.max_q), hist)
                    st.session_state.messages.append({"role": "assistant", "content": q})
                st.rerun()
            else:
                st.session_state.step = "analysis"
                st.rerun()

    # ЭТАП 4: Анализ
    elif st.session_state.step == "analysis":
        with st.spinner("Изолированный аудит GigaChat..."):
            transcript = "".join([f"{'Вопрос ИИ' if m['role']=='assistant' else 'Ответ Кандидата'}: {m['content']}\n" for m in st.session_state.messages])
            raw = giga.ask(get_final_analysis_prompt(st.session_state.role, st.session_state.pos, transcript), [])
            
            radar_data = {}
            json_match = re.search(r'```json\n(.*?)\n```', raw, re.DOTALL)
            if json_match:
                radar_data = json.loads(json_match.group(1))
                text_report = raw.replace(json_match.group(0), "").strip()
            else: text_report = raw

            rid = save_report(st.session_state.role, st.session_state.pos, st.session_state.messages, text_report, radar_data)
            st.success("Оценка завершена!")
            st.write("Передайте эту ссылку вашему HR-менеджеру:")
            st.code(f"https://your-app.streamlit.app/?report={rid}")
            if st.button("На главную"):
                for k in list(st.session_state.keys()): del st.session_state[k]
                st.rerun()


# ----------------------------------------
# КАБИНЕТ HR
# ----------------------------------------
def show_hr_view(report_id):
    st.title("HR-Дашборд Аналитики")
    
    if 'hr_auth' not in st.session_state: st.session_state.hr_auth = False

    if not st.session_state.hr_auth:
        expected = st.secrets.get("HR_PIN")
        if not expected:
            st.error("Ошибка: ПИН-код не настроен.")
            return
        pin = st.text_input("Введите PIN-код доступа:", type="password")
        if st.button("Войти"):
            if pin == expected:
                st.session_state.hr_auth = True
                st.rerun()
            else: st.error("Неверный код.")
        return

    data = get_report(report_id)
    if data:
        role, pos, hist_j, analysis, radar_j = data
        st.markdown(f"### Результат: {role}")
        st.info(f"**Позиция:** {pos}")

        radar_data = json.loads(radar_j)
        if radar_data:
            c1, c2 = st.columns(2)
            with c1: draw_gauge_chart(sum(radar_data.values())/len(radar_data))
            with c2: draw_radar_chart(radar_data)

        st.markdown("### Аналитическое заключение")
        st.markdown(analysis)

        # Формирование файла для скачивания
        messages = json.loads(hist_j)
        transcript_text = "\n".join([f"{'Система' if m['role']=='assistant' else 'Кандидат'}: {m['content']}" for m in messages])
        full_report = f"ПОЗИЦИЯ: {pos}\n\nЗАКЛЮЧЕНИЕ:\n{analysis}\n\n{'='*30}\nСТЕНОГРАММА:\n{transcript_text}"
        
        st.download_button("📄 Скачать полный отчет (+стенограмма)", full_report, file_name=f"Report_{report_id[:8]}.txt")

        with st.expander("Посмотреть стенограмму чата"):
            for m in messages:
                label = "🤖 Система" if m['role']=='assistant' else "👤 Кандидат"
                st.write(f"**{label}:** {m['content']}")
    else:
        st.error("Отчет не найден.")

if __name__ == "__main__":
    main()
