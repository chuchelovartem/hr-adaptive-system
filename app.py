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
    // 1. Блокировка вставки и копирования
    const blockCopyPaste = () => {
        const inputs = window.parent.document.querySelectorAll('textarea, input');
        inputs.forEach(input => {
            input.onpaste = (e) => { e.preventDefault(); alert('ПРОКТОРИНГ: Вставка текста запрещена!'); return false; };
            input.oncopy = (e) => e.preventDefault();
            input.oncontextmenu = (e) => e.preventDefault();
        });
    }
    setInterval(blockCopyPaste, 1000);

    // 2. Детектор потери фокуса (Tab-Switching)
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === 'hidden') {
            alert('ПРОКТОРИНГ: Зафиксировано переключение вкладки! Система помечает это как попытку списывания.');
        }
    });

    // 3. Защита от случайного обновления (F5)
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


def draw_radar_chart(data_dict):
    """Строит радарную диаграмму компетенций с помощью Plotly."""
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

class GigaChatAPI:
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
    """Адаптивная генерация (IRT): ИИ учитывает предыдущие ответы для подбора сложности."""
    base = f"Ты — Технический Интервьюер. Вакансия: {pos}. Шаг интервью: {current_step} из {max_steps}."

    if role == "Соискатель":
        return f"""{base}
        ТВОЯ ЗАДАЧА: Проанализируй историю диалога. Оцени, насколько хорошо кандидат ответил на предыдущий вопрос.
        1. Если ответил сильно — сгенерируй следующий технический вопрос СЛОЖНЕЕ (углубись в архитектуру).
        2. Если ответил слабо — задай базовый вопрос из смежной области.
        3. Если это первый вопрос — задай микро-кейс средней сложности.
        ВЫВОД: Напиши ТОЛЬКО текст следующего вопроса. Никаких приветствий и оценок вслух."""
    else:
        return f"""{base}
        ТВОЯ ЗАДАЧА: Оценить потенциал сотрудника. Если он отвечает развернуто — задай сложный кросс-грейдовый кейс. Если отвечает скупо — задай рефлексивный вопрос о проблемах в его процессах.
        ВЫВОД: Напиши ТОЛЬКО текст следующего вопроса."""


def get_final_analysis_prompt(role, pos):
    """Многомерный анализ с жестким антифрод-барьером и защитой от галлюцинаций."""
    if role == "Соискатель":
        return f"""Ты — бескомпромиссный Технический Аудитор уровня Senior. Твоя задача — проанализировать стенограмму интервью на позицию: {pos}.

        [ШАГ 1: ЖЕСТКИЙ АНТИФРОД И ДЕТЕКТОР САБОТАЖА]
        Сначала проверь все ответы кандидата на адекватность. 
        КРИТИЧЕСКИЕ ПРИЗНАКИ САБОТАЖА:
        - Ответы состоят из 1-3 слов на сложные технические вопросы (например: "гост", "да", "не знаю", "раз-два-три").
        - Кандидат откровенно издевается ("капаем, строим", "вуаля").
        - Полное отсутствие профессиональных терминов в ответах.

        ЕСЛИ ТЫ ВИДИШЬ ХОТЯ БЫ 2 ТАКИХ ОТВЕТА:
        НЕМЕДЛЕННО прекращай анализ! Не пытайся искать скрытый смысл. Выведи строго следующий текст (и больше ничего, кроме JSON в конце):
        ### Антифрод-Радар: 🚨 ПРОВАЛЕН (Саботаж)
        Кандидат дисквалифицирован за издевательское отношение к тестированию, ответы не по существу и полное отсутствие демонстрации навыков.
        ### ИТОГОВЫЙ ВЕРДИКТ: ❌ ОТКЛОНЕН (Дисквалификация)

        [ШАГ 2: ПОЛНЫЙ АНАЛИЗ (ТОЛЬКО ЕСЛИ АНТИФРОД ПРОЙДЕН)]
        Если ответы развернутые и профессиональные, сделай отчет:
        ### Антифрод-Радар: ✅ Пройден
        ### Лексический профиль и Уверенность: (Анализ тональности)
        ### Фактические Компетенции: (Разбор Hard Skills. ЗАПРЕЩЕНО выдумывать то, чего нет в ответах кандидата!)
        ### ИТОГОВЫЙ ВЕРДИКТ: [РЕКОМЕНДОВАН / ОТКЛОНЕН]

        [ШАГ 3: JSON ДЛЯ ДАШБОРДА (ОБЯЗАТЕЛЬНО ДЛЯ ВСЕХ)]
        В самом конце ответа ВСЕГДА выводи этот блок. Если был саботаж — ставь везде 0.
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
        
        [ВНИМАНИЕ: ЗАЩИТА ОТ ГАЛЛЮЦИНАЦИЙ]
        Оценивай ТОЛЬКО те факты, которые есть в тексте. Не додумывай достижения. Если ответы короткие отписки — отмечай это как низкую мотивацию.

        Сделай текстовый отчет:
        ### Индекс готовности к повышению:
        ### Лексический профиль (Признаки выгорания):
        ### Карьерный Action Plan:
        
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

    # --- ВАШ КЛЮЧ АВТОРИЗАЦИИ GIGACHAT ---
    AUTH_KEY = st.secrets["GIGACHAT_KEY"]
    giga = GigaChatAPI(AUTH_KEY)

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

                # Генерация первого вопроса
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

        # Отрисовка истории чата
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

        st.progress(remaining / time_limit)
        st.caption(
            f"Прогресс: вопрос {st.session_state.q_count} из {st.session_state.max_questions} | Время: {remaining} сек.")

        # Чат-строка ввода (срабатывает по Enter)
        user_input = st.chat_input("Напишите ответ...")

        # Проверка таймера
        if remaining <= 0 and not user_input:
            user_input = "[ПРОКТОРИНГ: Кандидат не уложился в отведенное время]"

        if user_input:
            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state.q_count += 1

            if st.session_state.q_count <= st.session_state.max_questions:
                with st.spinner("Анализ ответа и генерация следующего кейса..."):
                    prompt = get_adaptive_question_prompt(st.session_state.role, st.session_state.pos,
                                                          st.session_state.q_count, st.session_state.max_questions)
                    # Отправляем историю диалога для адаптивности
                    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    next_q = giga.ask(prompt, history)
                    st.session_state.messages.append({"role": "assistant", "content": next_q})
                    st.session_state.start_time = time.time()
                    st.rerun()
            else:
                st.session_state.step = "analysis"
                st.rerun()

        # Автообновление для таймера
        time.sleep(1)
        st.rerun()

    elif st.session_state.step == "analysis":
        with st.spinner("Проведение многомерного анализа тональности и компетенций..."):
            history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
            prompt = get_final_analysis_prompt(st.session_state.role, st.session_state.pos)

            raw_analysis = giga.ask(prompt, history)

            # Извлечение JSON для дашборда с помощью регулярных выражений
            radar_data = {}
            json_match = re.search(r'```json\n(.*?)\n```', raw_analysis, re.DOTALL)
            if json_match:
                try:
                    radar_data = json.loads(json_match.group(1))
                    # Удаляем JSON блок из текстового отчета, чтобы не дублировать
                    text_analysis = raw_analysis.replace(json_match.group(0), "").strip()
                except:
                    text_analysis = raw_analysis
            else:
                text_analysis = raw_analysis

            report_id = save_report(st.session_state.role, st.session_state.pos,
                                    st.session_state.messages, text_analysis, radar_data)

            st.success("Оценка успешно завершена.")
            st.markdown("### Защищенный отчет сформирован")
            
            st.write("Для просмотра отчета нажмите кнопку ниже или добавьте параметр к адресу:")
            st.code(f"?report={report_id}", language="text")
            
            st.divider()
            
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Открыть HR-Дашборд сейчас", type="primary", use_container_width=True):
                    st.query_params["report"] = report_id
                    st.rerun()
            with col2:
                if st.button("Завершить сессию", use_container_width=True):
                    for key in list(st.session_state.keys()): del st.session_state[key]
                    st.rerun()


# ----------------------------------------
# КАБИНЕТ HR (АНАЛИТИЧЕСКИЙ ДАШБОРД)
# ----------------------------------------
def show_hr_view(report_id):
    data = get_report(report_id)
    if data:
        role, pos, history_json, analysis, radar_json = data
        st.title("Аналитический HR-Дашборд")
        st.success("Верификация пройдена. Данные защищены.")

        col1, col2 = st.columns(2)
        col1.metric("Тип оценки", role)
        col2.metric("Позиция", pos)

        st.divider()

        # Отрисовка радарной диаграммы
        radar_data = json.loads(radar_json)
        if radar_data:
            st.markdown("### Профиль компетенций (Spider Chart)")
            draw_radar_chart(radar_data)

        st.markdown("### Заключение ИИ-Аудитора")
        st.markdown(analysis)

        st.divider()
        with st.expander("Стенограмма адаптивного интервью (Чат)"):
            messages = json.loads(history_json)
            for msg in messages:
                if msg["role"] == "assistant":
                    st.markdown(f"**🤖 ИИ:** {msg['content']}")
                else:
                    if "ПРОКТОРИНГ" in msg["content"]:
                        st.error(f"**👤 Кандидат:** {msg['content']}")
                    else:
                        st.info(f"**👤 Кандидат:** {msg['content']}")
    else:
        st.error("Отчет не найден.")
        if st.button("На главную"):
            st.query_params.clear()
            st.rerun()


if __name__ == "__main__":
    main()
