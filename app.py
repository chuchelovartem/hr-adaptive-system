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
            alert('ПРОКТОРИНГ: Зафиксировано переключение вкладки! Система помечает это как попытку списывания.');
        }
    });

    window.addEventListener("beforeunload", (e) => {
        e.preventDefault();
        e.returnValue = 'Вы уверены, что хотите покинуть страницу? Прогресс тестирования может быть утерян.';
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
    """Строит спидометр (Gauge Chart) для отображения общего уровня."""
    if score < 4:
        level, color = "Слабый", "#E74C3C"
    elif score < 8:
        level, color = "Средний", "#F39C12"
    else:
        level, color = "Сильный", "#27AE60"

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        domain={'x': [0, 1], 'y': [0, 1]},
        title={'text': f"Общий уровень компетенций: {level}", 'font': {'size': 20}},
        gauge={
            'axis': {'range': [0, 10], 'tickwidth': 1, 'tickcolor': "darkblue"},
            'bar': {'color': color},
            'bgcolor': "white",
            'borderwidth': 2,
            'bordercolor': "gray",
            'steps': [
                {'range': [0, 3.9], 'color': "rgba(231, 76, 60, 0.2)"},
                {'range': [4, 7.9], 'color': "rgba(243, 156, 18, 0.2)"},
                {'range': [8, 10], 'color': "rgba(39, 174, 96, 0.2)"}
            ]
        }
    ))
    fig.update_layout(margin=dict(l=20, r=20, t=50, b=20), height=300)
    st.plotly_chart(fig, use_container_width=True)


def draw_radar_chart(data_dict):
    """Строит радарную диаграмму компетенций."""
    categories = list(data_dict.keys())
    values = list(data_dict.values())

    categories.append(categories[0])
    values.append(values[0])

    fig = go.Figure(data=go.Scatterpolar(
        r=values,
        theta=categories,
        fill='toself',
        line_color='#2E86C1',
        fillcolor='rgba(46, 134, 193, 0.4)'
    ))
    fig.update_layout(
        polar=dict(radialaxis=dict(visible=True, range=[0, 10])),
        showlegend=False,
        margin=dict(l=40, r=40, t=40, b=40)
    )
    st.plotly_chart(fig, use_container_width=True)


# ==========================================
# 3. ИНТЕГРАЦИЯ GIGACHAT И ПРОМПТЫ (IRT & NLP)
# ==========================================

class GigaChatIntegration:
    def __init__(self, auth_key):
        self.auth_key = auth_key
        self.token = self._get_token()
        self.base_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def _get_token(self):
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'RqUID': str(uuid.uuid4()),
            'Authorization': f'Basic {self.auth_key}'
        }
        res = requests.post(url, headers=headers, data={'scope': 'GIGACHAT_API_PERS'}, verify=False)
        if res.status_code == 200:
            return res.json().get('access_token')
        return None

    def ask(self, system_prompt, history):
        if not self.token: return "Ошибка авторизации."
        messages = [{"role": "system", "content": system_prompt}] + history
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        payload = {"model": "GigaChat", "messages": messages, "temperature": 0.6}
        res = requests.post(self.base_url, headers=headers, json=payload, verify=False)
        if res.status_code == 200:
            return res.json()['choices'][0]['message']['content']
        return "Ошибка API."


def get_adaptive_question_prompt(role, pos, current_step, max_steps):
    """Адаптивная генерация (IRT): ИИ задает короткие, точечные вопросы без воды."""
    base = f"Ты — строгий Технический Интервьюер. Вакансия: {pos}. Шаг интервью: {current_step} из {max_steps}."

    return f"""{base}
    ТВОЯ ЗАДАЧА: Проанализировать предыдущий ответ и задать следующий вопрос. 
    
    ПРАВИЛА ГЕНЕРАЦИИ ВОПРОСА (НАРУШЕНИЕ КАРАЕТСЯ СБОЕМ СИСТЕМЫ):
    1. Задай ТОЛЬКО ОДИН точечный вопрос, требующий понимания предметной области.
    2. Вопрос должен быть сформулирован так, чтобы кандидат мог ответить на него максимум одним-двумя предложениями.
    3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать свои рассуждения, оценивать предыдущий ответ вслух, использовать эмодзи, писать приветствия или слова вроде "🤖 ИИ:", "Вопрос:".
    4. Если кандидат отвечает слабо — задай базовый вопрос. Если сильно — усложни уровень (углубление в архитектуру/процессы).
    
    ВЫВОД: СТРОГО текст вопроса (одно предложение) и больше ничего."""


def get_final_analysis_prompt(role, pos, transcript):
    """Многомерный анализ: Строгий академический стиль без эмодзи."""
    
    base_rules = """
    [ПРАВИЛА ЖЕСТКОГО АУДИТА — КРИТИЧЕСКИ ВАЖНО!]
    1. Оценивай ТОЛЬКО текст, написанный после слов "Ответ Кандидата:". 
    2. ЗАПРЕЩЕНО использовать термины из текста "Вопрос ИИ:" для оценки знаний кандидата.
    3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать эмодзи (смайлики) и неформальную лексику. Используй строгий академический деловой стиль.
    4. Если кандидат отвечает отписками или не использует профессиональные термины — это САБОТАЖ и НОЛЬ ЗНАНИЙ.
    """

    if role == "Соискатель":
        return f"""Ты — бескомпромиссный Технический Аудитор. Проведи аудит стенограммы интервью на позицию: {pos}.

[СТЕНОГРАММА ИНТЕРВЬЮ]
{transcript}
[КОНЕЦ СТЕНОГРАММЫ]
{base_rules}

[АЛГОРИТМ ПРИНЯТИЯ РЕШЕНИЯ]
ЕСЛИ большинство ответов — отписки или бессмысленные фразы:
Выведи СТРОГО этот текст (без эмоций):
Антифрод-Радар: ПРОВАЛЕН (Саботаж/Отписки).
Лексический профиль: Низкая деловая культура.
Фактические Компетенции: 0 из 10. Кандидат не дал ни одного содержательного технического ответа.
ИТОГОВЫЙ ВЕРДИКТ: ОТКЛОНЕН (Дисквалификация).

ЕСЛИ ответы развернутые и профессиональные:
Сделай стандартный отчет (Антифрод, Лексика, Оценка компетенций с цитатами, Итоговый Вердикт). Укажи приблизительный уровень компетенций (Слабый, Средний или Сильный).

[ОБЯЗАТЕЛЬНЫЙ JSON В КОНЦЕ]
```json
{{
    "Hard_Skills": 0,
    "Когнитивная_Гибкость": 0,
    "Уверенность_и_Лидерство": 0,
    "Профессиональная_Этика": 0,
    "Системное_Мышление": 0
}}
```"""
    else:
        return f"""Ты — Эксперт по карьерному развитию. Проанализируй диалог сотрудника ({pos}).
        
[СТЕНОГРАММА ИНТЕРВЬЮ]
{transcript}
[КОНЕЦ СТЕНОГРАММЫ]
{base_rules}

Сделай подробный текстовый отчет в академическом стиле:
Индекс готовности к повышению: (Укажи уровень: Слабый, Средний или Сильный)
Лексический профиль:
Карьерный Action Plan:

В конце отчета выведи JSON-блок (оценки 0-10):
```json
{{
    "Проактивность": 5,
    "Бизнес_Видение": 5,
    "Стрессоустойчивость": 5,
    "Мотивация": 5,
    "Самостоятельность": 5
}}
```"""


# ==========================================
# 4. ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС (ЧАТ-ФОРМАТ)
# ==========================================

def main():
    st.set_page_config(page_title="Адаптивная платформа оценки", layout="centered")
    init_db()

    AUTH_KEY = st.secrets.get("GIGACHAT_KEY", "ВАШ_КЛЮЧ")
    giga = GigaChatIntegration(AUTH_KEY)

    if "report" in st.query_params:
        show_hr_view(st.query_params["report"])
        return

    inject_proctoring_js()

    st.title("Интеллектуальная система оценки")

    if 'step' not in st.session_state:
        st.session_state.step = "role_selection"
        st.session_state.messages = []
        st.session_state.max_questions = 0
        st.session_state.q_count = 0
        st.session_state.start_time = 0

    if st.session_state.step == "role_selection":
        st.markdown("Выберите профиль для начала адаптивного интервью:")
        col1, col2 = st.columns(2)
        if col1.button("Соискатель", use_container_width=True):
            st.session_state.role = "Соискатель"
            st.session_state.max_questions = 8
            st.session_state.step = "pos_input"
            st.rerun()
        if col2.button("Сотрудник", use_container_width=True):
            st.session_state.role = "Сотрудник"
            st.session_state.max_questions = 4
            st.session_state.step = "pos_input"
            st.rerun()

    elif st.session_state.step == "pos_input":
        label = "Укажите целевую вакансию:" if st.session_state.role == "Соискатель" else "Ваша текущая должность:"
        pos = st.text_input(label)
        if st.button("Запустить адаптивное интервью"):
            if pos.strip():
                st.session_state.pos = pos
                st.session_state.step = "interview"
                st.session_state.q_count = 1

                with st.spinner("Анализ профиля..."):
                    prompt = get_adaptive_question_prompt(st.session_state.role, pos, 1, st.session_state.max_questions)
                    first_q = giga.ask(prompt, [])
                    st.session_state.messages.append({"role": "assistant", "content": first_q})
                    st.session_state.start_time = time.time()
                st.rerun()

    elif st.session_state.step == "interview":
        time_limit = 90 if st.session_state.role == "Соискатель" else 120
        elapsed = time.time() - st.session_state.start_time
        remaining = max(0, time_limit - int(elapsed))

        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

        st.progress(remaining / time_limit)
        st.caption(f"Прогресс: вопрос {st.session_state.q_count} из {st.session_state.max_questions} | Время: {remaining} сек.")

        user_input = st.chat_input("Напишите ответ...")

        if remaining <= 0 and not user_input:
            user_input = "[ПРОКТОРИНГ: Кандидат не уложился в отведенное время]"

        if user_input:
            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state.q_count += 1

            if st.session_state.q_count <= st.session_state.max_questions:
                with st.spinner("Анализ ответа и генерация следующего кейса..."):
                    prompt = get_adaptive_question_prompt(st.session_state.role, st.session_state.pos,
                                                          st.session_state.q_count, st.session_state.max_questions)
                    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    next_q = giga.ask(prompt, history)
                    st.session_state.messages.append({"role": "assistant", "content": next_q})
                    st.session_state.start_time = time.time()
                    st.rerun()
            else:
                st.session_state.step = "analysis"
                st.rerun()

        time.sleep(1)
        st.rerun()

    elif st.session_state.step == "analysis":
        with st.spinner("Проведение изолированного многомерного анализа..."):
            transcript_text = ""
            for m in st.session_state.messages:
                speaker = "Вопрос ИИ" if m["role"] == "assistant" else "Ответ Кандидата"
                transcript_text += f"{speaker}:\n{m['content']}\n\n"
            
            prompt = get_final_analysis_prompt(st.session_state.role, st.session_state.pos, transcript_text)
            raw_analysis = giga.ask(prompt, []) 

            radar_data = {}
            json_match = re.search(r'```json\n(.*?)\n```', raw_analysis, re.DOTALL)
            if json_match:
                try:
                    radar_data = json.loads(json_match.group(1))
                    text_analysis = raw_analysis.replace(json_match.group(0), "").strip()
                except:
                    text_analysis = raw_analysis
            else:
                text_analysis = raw_analysis

            report_id = save_report(st.session_state.role, st.session_state.pos,
                                    st.session_state.messages, text_analysis, radar_data)

            st.success("Оценка успешно завершена.")
            st.markdown("### Данные сохранены")
            st.write("Пожалуйста, скопируйте ссылку ниже и передайте её вашему HR-менеджеру для проверки результатов:")
            
            # Генерация абсолютной ссылки (можно заменить на ваш реальный домен)
            base_url = "https://your-app-domain.streamlit.app" 
            report_url = f"{base_url}/?report={report_id}"
            
            st.code(report_url, language="text")
            
            st.divider()
            
            if st.button("Завершить сессию и вернуться на главную", use_container_width=True):
                for key in list(st.session_state.keys()): del st.session_state[key]
                st.rerun()


# ----------------------------------------
# КАБИНЕТ HR (АНАЛИТИЧЕСКИЙ ДАШБОРД)
# ----------------------------------------
def show_hr_view(report_id):
    st.title("Аналитический HR-Дашборд")
    
    # 1. Слой безопасности HR (ПИН-КОД)
    if 'hr_authorized' not in st.session_state:
        st.session_state.hr_authorized = False

    if not st.session_state.hr_authorized:
        st.warning("Внимание: Раздел защищен. Доступ только для сотрудников отдела кадров.")
        pin_code = st.text_input("Введите PIN-код для доступа (по умолчанию: 1234)", type="password")
        # Пытаемся получить ПИН из секретов, если его нет — доступ будет невозможен
        expected_pin = st.secrets.get("HR_PIN") 

        if not expected_pin:
        st.error("Ошибка конфигурации: ПИН-код администратора не установлен.")
        return
        
        if st.button("Подтвердить"):
            if pin_code == expected_pin:
                st.session_state.hr_authorized = True
                st.rerun()
            else:
                st.error("Ошибка верификации: Неверный PIN-код.")
        return

    # 2. Отрисовка Дашборда после авторизации
    data = get_report(report_id)
    if data:
        role, pos, history_json, analysis, radar_json = data
        st.success("Верификация пройдена. Данные защищены.")

        col1, col2 = st.columns(2)
        col1.metric("Тип оценки", role)
        # Использование markdown для поддержки длинных названий с переносом строк
        with col2:
            st.markdown(f"**Целевая позиция:**<br>{pos}", unsafe_allow_html=True)

        st.divider()

        radar_data = json.loads(radar_json)
        if radar_data:
            # Расчет среднего балла для спидометра
            avg_score = sum(radar_data.values()) / len(radar_data) if radar_data else 0
            
            col_chart1, col_chart2 = st.columns(2)
            with col_chart1:
                draw_gauge_chart(avg_score)
            with col_chart2:
                draw_radar_chart(radar_data)

        st.markdown("### Заключение ИИ-Аудитора")
        
        # Добавляем возможность выгрузки отчета в txt (Альтернатива PDF для легкого скачивания)
        st.download_button(
            label="📄 Скачать текстовый отчет",
            data=f"ПОЗИЦИЯ: {pos}\n\nЗАКЛЮЧЕНИЕ:\n{analysis}",
            file_name=f"HR_Report_{report_id[:8]}.txt",
            mime="text/plain"
        )
        
        st.markdown(analysis)

        st.divider()
        with st.expander("Стенограмма адаптивного интервью (Чат)"):
            messages = json.loads(history_json)
            for msg in messages:
                if msg["role"] == "assistant":
                    # Убрано эмодзи и префикс для более строгого вида
                    st.markdown(f"**Система:** {msg['content']}")
                else:
                    if "ПРОКТОРИНГ" in msg["content"]:
                        st.error(f"**Кандидат:** {msg['content']}")
                    else:
                        st.info(f"**Кандидат:** {msg['content']}")
    else:
        st.error("Отчет не найден в базе данных.")
        if st.button("На главную"):
            st.query_params.clear()
            st.rerun()


if __name__ == "__main__":
    main()
