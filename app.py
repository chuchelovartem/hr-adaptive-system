import os, uuid, json, sqlite3, time, re, requests
import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go

# ==========================================
# 1. СТЕЛС-ПРОКТОРИНГ И ЗАЩИТА ИНТЕРФЕЙСА
# ==========================================
def inject_proctoring_js():
    js_code = """
    <script>
    let cheatCount = parseInt(new URL(window.parent.location.href).searchParams.get('_v_idx') || '0');
    let isReady = false;
    
    let checkReady = setInterval(() => {
        if(window.parent.document.readyState === 'complete') { isReady = true; clearInterval(checkReady); }
    }, 500);
    
    const blockCopyPaste = () => {
        const inputs = window.parent.document.querySelectorAll('textarea, input');
        inputs.forEach(input => {
            input.onpaste = (e) => { e.preventDefault(); return false; };
            input.oncopy = (e) => e.preventDefault();
            input.oncontextmenu = (e) => e.preventDefault();
        });
    }
    setInterval(blockCopyPaste, 1000);
    
    const recordCheat = () => {
        if (isReady) {
            cheatCount++;
            const url = new URL(window.parent.location.href);
            url.searchParams.set('_v_idx', cheatCount);
            window.parent.history.pushState({}, '', url);
        }
    };
    
    const parentWindow = window.parent;
    const parentDoc = window.parent.document;
    
    parentDoc.addEventListener("visibilitychange", () => { 
        if (parentDoc.visibilityState === 'hidden') recordCheat(); 
    });
    parentWindow.addEventListener("blur", () => { recordCheat(); });
    </script>
    """
    components.html(js_code, height=0)

# ==========================================
# 2. БАЗА ДАННЫХ И ВИЗУАЛИЗАЦИЯ
# ==========================================
def init_db():
    conn = sqlite3.connect('hr_platform_final.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS adaptive_reports 
                 (id TEXT PRIMARY KEY, role_type TEXT, target_pos TEXT, dialog_history TEXT, 
                  analysis_text TEXT, radar_data TEXT, cheat_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def save_report(role, pos, history, analysis, radar_data, cheat_count):
    report_id = str(uuid.uuid4())
    conn = sqlite3.connect('hr_platform_final.db')
    c = conn.cursor()
    c.execute("INSERT INTO adaptive_reports (id, role_type, target_pos, dialog_history, analysis_text, radar_data, cheat_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (report_id, role, pos, json.dumps(history, ensure_ascii=False), analysis, json.dumps(radar_data, ensure_ascii=False), cheat_count))
    conn.commit()
    conn.close()
    return report_id

def get_report(report_id):
    conn = sqlite3.connect('hr_platform_final.db')
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
    if categories and values:
        categories.append(categories[0])
        values.append(values[0])
    
    fig = go.Figure(data=go.Scatterpolar(r=values, theta=categories, fill='toself', line_color='#2E86C1'))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 10])), height=300, margin=dict(l=40, r=40, t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)

# ==========================================
# 3. БИЗНЕС-ЛОГИКА ПРОКТОРИНГА НА PYTHON
# ==========================================
def apply_proctoring_penalty(radar_data, cheat_count, role):
    if cheat_count > 0:
        penalty = min(cheat_count, 3) 
        if role == "Соискатель":
            current = radar_data.get("Устойчивость_к_проверке", 5)
            radar_data["Устойчивость_к_проверке"] = max(1, current - penalty)
        else:
            current = radar_data.get("Адаптивность", 5)
            radar_data["Адаптивность"] = max(1, current - penalty)
    return radar_data

# ==========================================
# 4. ИНТЕГРАЦИЯ И ПРОМПТ-ИНЖИНИРИНГ (GIGACHAT)
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
            return requests.post(url, headers=headers, data={'scope': 'GIGACHAT_API_PERS'}, verify=False).json().get('access_token')
        except: 
            return None

    def ask(self, system_prompt, history, temperature=0.6):
        if not self.token: 
            return "Ошибка авторизации API GigaChat."
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        payload = {
            "model": "GigaChat", 
            "messages": [{"role": "system", "content": system_prompt}] + history, 
            "temperature": temperature,
            "max_tokens": 2048
        }
        res = requests.post(self.url, headers=headers, json=payload, verify=False)
        return res.json()['choices'][0]['message']['content'] if res.status_code == 200 else "Сбой генерации ответа."

def get_adaptive_question_prompt(role, pos, jd_context, step, max_steps):
    context_block = f"\nДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ ВАКАНСИИ/ПРОФИЛЯ:\n{jd_context}\n" if jd_context else ""
    
    grade_adaptation = f"""
        [ОМНИ-АДАПТАЦИЯ ПОД ДОЛЖНОСТЬ: {pos} - КРИТИЧЕСКИ ВАЖНО]
        Сценарии должны на 100% соответствовать реалиям этой профессии:
        - РАБОЧИЙ/ЛИНЕЙНЫЙ ПЕРСОНАЛ: Бытовые физические ситуации (сломался инструмент, недовольный клиент). Строго без бизнес-лексики.
        - ИТ/НАУКА/ИНЖЕНЕРИЯ: Технические задачи, алгоритмы, методология исследований, сбои в архитектуре.
        - УПРАВЛЕНИЕ/ОФИС: Бизнес-кейсы, конфликты ресурсов, оптимизация метрик.
        - КРЕАТИВ: Брифы, правки, творческие блоки.
    """
    
    time_instruction = """
        [ТАЙМИНГ И ФОРМАТ ОТВЕТА - ЖЕСТКОЕ ПРАВИЛО]
        У пользователя есть всего 90-120 секунд на чтение вопроса и печать ответа. 
        Твой вопрос должен быть конкретным микро-кейсом. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО просить "описать детально" или "написать эссе". 
        Спрашивай об 1-2 ключевых шагах, инструментах или решениях.
    """
    
    if role == "Соискатель":
        domains = [
            "Фундаментальные профессиональные знания", 
            "Решение нестандартных или кризисных ситуаций", 
            "Профильные инструменты и технологии", 
            "Проверка поведенческого опыта (STAR)", 
            "Оценка рисков и процессов", 
            "Мотивация и культурный фит"
        ]
        current_domain = domains[min(step-1, len(domains)-1)]
        
        return f"""Ты — профессиональный нанимающий менеджер. Идет блиц-интервью на позицию: {pos}.{context_block}
        ШАГ: {step}/{max_steps}. Твоя НОВАЯ область проверки: "{current_domain}".{grade_adaptation}{time_instruction}
        
        [ФОРМАТ ВОПРОСА]
        Твой ответ должен выглядеть СТРОГО ТАК:
        **Ситуация:** (1 предложение о рабочей проблеме)
        **Вопрос:** (1 конкретный, открытый вопрос)
        
        [ЗАПРЕТЫ] СТРОГО ОДИН ВОПРОС. ЗАПРЕЩЕНО оценивать прошлый ответ."""
    else:
        scenario = [
            "Ретроспектива (Оценка недавней сложной задачи)", 
            "Оценка потенциала (Моделирование задачи следующего грейда)", 
            "Аудит дефицитов (Нехватка компетенций)", 
            "Адаптивность (Изменение правил/технологий)", 
            "Вектор развития (Освоение нового)", 
            "Deep Dive"
        ]
        curr_task = scenario[min(step-1, len(scenario)-1)]
        
        return f"""Ты — HR-коуч. Идет аудит сотрудника: {pos}.{context_block} Шаг: {step}/{max_steps}. Этап оценки: {curr_task}.{grade_adaptation}{time_instruction}
        
        [ФОРМАТ ВОПРОСА]
        Твой ответ должен выглядеть СТРОГО так:
        **Ситуация:** (1 короткая вводная мысль по этапу)
        **Вопрос:** (1 конкретный вопрос для быстрого ответа)
        
        [БРОНЯ] Мягко возвращай к теме, если сотрудник уходит от ответа, без оценки прошлых слов."""

def get_final_analysis_prompt(role, pos, jd_context, transcript):
    context_block = f"\n[БИЗНЕС-КОНТЕКСТ (ISO 30405)]:\n{jd_context}\n" if jd_context else ""
    anti_break_rule = "\n[КРИТИЧЕСКАЯ ИНСТРУКЦИЯ] ЗАПРЕЩЕНО использовать фигурные скобки { и } в тексте. Используй их ТОЛЬКО один раз в самом конце для JSON. ЗАПРЕЩЕНО рисовать таблицы (используй списки и абзацы)."
    
    anti_bias_instruction = f"""
        [ЗАЩИТА ОТ ИСКАЖЕНИЙ (ANTI-BIAS) ПО ДОЛЖНОСТИ: {pos} - КРИТИЧЕСКИ ВАЖНО]
        - Для РАБОЧИХ/ЛИНЕЙНЫХ профессий: ЗАПРЕЩЕНО штрафовать за краткость. Оценивай СУТЬ физических/практических действий. Если предложено логичное, безопасное и эффективное решение — это эталонный ответ на 9-10 баллов.
        - Для ИНТЕЛЛЕКТУАЛЬНЫХ/УПРАВЛЕНЧЕСКИХ/НАУЧНЫХ профессий: Оценивай глубину экспертизы, системный подход, учет рисков и методологию.
    """
    
    if role == "Соискатель":
        return f"""Ты — объективный HR-директор. Проведи аудит интервью на позицию {pos}.{context_block}{anti_break_rule}{anti_bias_instruction}
        
        [МЕТОДОЛОГИЯ ОЦЕНКИ BARS]
        1-3: Ошибается в базовых вещах, создает риски для процессов.
        4-6: Знает базу, но плавает в нестандартных ситуациях.
        7-8: Приводит верные практические примеры, решает задачи алгоритмически правильно.
        9-10: Предлагает наиболее эффективные, безопасные и комплексные решения.
        
        [СТРУКТУРА ОТВЕТА]
        ШАГ 1: ТЕКСТОВЫЙ ОТЧЕТ
        - Резюме компетенций (с доказательствами из текста).
        - Сильные стороны.
        - Зоны риска.
        - Вердикт (Да/Нет/С условием).

        ШАГ 2: ФИНАЛЬНАЯ ОЦЕНКА В ФОРМАТЕ JSON
        Выведи JSON СТРОГО в самом конце. Ключи: "Техническая_Точность", "Скорость_Мышления", "Практический_Опыт", "Лаконичность", "Устойчивость_к_проверке".

        [FEW-SHOT ПРИМЕР JSON]
        ```json
        {{"Техническая_Точность": 8, "Скорость_Мышления": 7, "Практический_Опыт": 6, "Лаконичность": 9, "Устойчивость_к_проверке": 8}}
        ```

        [СТЕНОГРАММА ИНТЕРВЬЮ]
        {transcript}"""
    else:
        return f"""Ты — корпоративный HR-методолог. Проведи аудит действующего сотрудника на позиции: {pos}.{context_block}{anti_break_rule}{anti_bias_instruction}
        
        [МЕТОДОЛОГИЯ ОЦЕНКИ BARS]
        1-3: Делает ошибки, избегает ответственности, сопротивляется новому.
        4-6: Стабильный исполнитель. Выполняет норму, но не проявляет инициативы.
        7-8: Самостоятелен, надежен, может подстраховать коллег или решать нестандартные задачи.
        9-10: Берет ответственность за результат системы/процесса, драйвит изменения (Максимальный потенциал).
        
        [СТРУКТУРА ОТВЕТА]
        ШАГ 1: ТЕКСТОВЫЙ ОТЧЕТ (Сплошным текстом и списками!)
        - Матрица талантов (Результативность и Потенциал): Текстовое описание соотношения текущих результатов сотрудника и его потенциала к росту.
        - Дефицит компетенций (Skill-Gap): Чего не хватает для перехода на следующий профессиональный/управленческий уровень.
        - Индивидуальный план развития (ИПР): 3 конкретных, понятных шага.
        - Вердикт.

        ШАГ 2: ФИНАЛЬНАЯ ОЦЕНКА В ФОРМАТЕ JSON
        Выведи JSON СТРОГО в самом конце. Ключи: "Результативность", "Обучаемость", "Лидерство", "Адаптивность", "Экспертиза".

        [FEW-SHOT ПРИМЕР JSON]
        ```json
        {{"Результативность": 7, "Обучаемость": 8, "Лидерство": 5, "Адаптивность": 6, "Экспертиза": 8}}
        ```

        [СТЕНОГРАММА]
        {transcript}"""

# ==========================================
# 5. ИНТЕРФЕЙС И ГЛАВНЫЙ ЦИКЛ
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
        st.session_state.update({'step': "role_selection", 'messages': [], 'q_count': 0, 'jd_context': ""})

    if st.session_state.step == "role_selection":
        c1, c2 = st.columns(2)
        if c1.button("Соискатель", use_container_width=True): 
            st.session_state.update({'role': "Соискатель", 'max_q': 6, 'step': "pos_input"})
            st.rerun()
        if c2.button("Сотрудник", use_container_width=True): 
            st.session_state.update({'role': "Сотрудник", 'max_q': 6, 'step': "pos_input"})
            st.rerun()

    elif st.session_state.step == "pos_input":
        pos = st.text_input("Укажите позицию/должность:")
        jd_context = st.text_area("Дополнительное описание вакансии (опционально):", height=100)
        
        if st.button("Начать") and pos.strip():
            st.session_state.update({'pos': pos, 'jd_context': jd_context, 'step': "interview", 'q_count': 1})
            st.query_params.clear()
            
            with st.spinner("Генерация первого вопроса..."):
                q = giga.ask(get_adaptive_question_prompt(st.session_state.role, pos, jd_context, 1, st.session_state.max_q), [], temperature=0.6)
                st.session_state.messages.append({"role": "assistant", "content": q})
                st.session_state.start_time = time.time()
            st.rerun()

    elif st.session_state.step == "interview":
        time_limit = 90 if st.session_state.role == "Соискатель" else 120
        elapsed = time.time() - st.session_state.start_time
        remaining = max(0, time_limit - int(elapsed))

        for m in st.session_state.messages:
            with st.chat_message(m["role"]): 
                st.write(m["content"])
        
        st.progress(remaining / time_limit)
        st.caption(f"Вопрос {st.session_state.q_count} из {st.session_state.max_q} | Осталось времени: {remaining} сек.")

        user_input = st.chat_input("Ваш лаконичный ответ...")
        
        if remaining <= 0 and not user_input:
            user_input = "[ПРОКТОРИНГ: Кандидат не уложился в отведенное время]"

        if user_input:
            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state.q_count += 1
            
            if st.session_state.q_count <= st.session_state.max_q:
                with st.spinner("Анализ ответа..."):
                    hist = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    q = giga.ask(get_adaptive_question_prompt(st.session_state.role, st.session_state.pos, st.session_state.jd_context, st.session_state.q_count, st.session_state.max_q), hist, temperature=0.6)
                    st.session_state.messages.append({"role": "assistant", "content": q})
                    st.session_state.start_time = time.time() 
                st.rerun()
            else:
                st.session_state.step = "analysis"
                st.rerun()

        time.sleep(1)
        st.rerun()

    elif st.session_state.step == "analysis":
        with st.spinner("Формирование финального HR-аудита..."):
            cheat_count = int(st.query_params.get("_v_idx", 0))
            transcript = "".join([f"{'ИИ' if m['role']=='assistant' else 'Кандидат'}: {m['content']}\n" for m in st.session_state.messages])
            
            raw = giga.ask(get_final_analysis_prompt(st.session_state.role, st.session_state.pos, st.session_state.jd_context, transcript), [], temperature=0.2)
            
            start_idx = raw.find('{')
            end_idx = raw.rfind('}')
            
            if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                json_str = raw[start_idx:end_idx+1]
                text_report = re.split(r'(?i)ШАГ 2', raw[:start_idx])[0].strip()
                text_report = text_report.replace("```json", "").replace("```", "").strip()
            else:
                json_str = "{}"
                text_report = re.split(r'(?i)ШАГ 2', raw)[0].strip()
                text_report = text_report.replace("```json", "").replace("```", "").strip()

            try:
                radar_data = json.loads(json_str)
                if not radar_data: raise ValueError("Empty JSON")
                radar_data = apply_proctoring_penalty(radar_data, cheat_count, st.session_state.role)
            except:
                if st.session_state.role == "Соискатель":
                    radar_data = {"Техническая_Точность": 0, "Скорость_Мышления": 0, "Практический_Опыт": 0, "Лаконичность": 0, "Устойчивость_к_проверке": 0} 
                else:
                    radar_data = {"Результативность": 0, "Обучаемость": 0, "Лидерство": 0, "Адаптивность": 0, "Экспертиза": 0}

            rid = save_report(st.session_state.role, st.session_state.pos, st.session_state.messages, text_report, radar_data, cheat_count)
            st.success("Интервью успешно завершено.")
            
            app_domain = "https://adaptive-hr-system.streamlit.app"
            st.code(f"{app_domain}/?report={rid}")
            
            if st.button("На главную"):
                st.query_params.clear()
                for k in list(st.session_state.keys()): del st.session_state[k]
                st.rerun()

def show_hr_view(report_id):
    st.title("HR-Аналитика и Прокторинг")
    expected = st.secrets.get("HR_PIN", "1234")
    
    if 'hr_auth' not in st.session_state: 
        st.session_state.hr_auth = False
    
    if not st.session_state.hr_auth:
        with st.form("hr_login"):
            pin = st.text_input("Введите PIN-код для доступа к аналитике:", type="password")
            submitted = st.form_submit_button("Войти (HR Доступ)")
            if submitted:
                if pin == expected:
                    st.session_state.hr_auth = True
                    st.rerun()
                else:
                    st.error("Неверный PIN-код")
        return

    data = get_report(report_id)
    if data:
        role, pos, hist_j, analysis, radar_j, cheat_count = data
        st.markdown(f"### {role}: {pos}")
        st.divider()
        
        color = "inverse" if cheat_count > 0 else "normal"
        metric_label = "Потеря фокуса (переключение окон)" if role == "Соискатель" else "Потеря фокуса (отвлечение)"
        delta_label = "🚨 Внимание: Риск списывания" if (role == "Соискатель" and cheat_count > 0) else ("🚨 Внимание: Риск потери фокуса" if cheat_count > 0 else "✅ Ок")
        st.metric(metric_label, f"{cheat_count} раз", delta=delta_label, delta_color=color)
        st.divider()
        
        radar_data = json.loads(radar_j)
        if radar_data:
            c1, c2 = st.columns(2)
            is_error = all(v == 0 for v in radar_data.values())
            if is_error:
                st.error("⚠️ ВНИМАНИЕ: Ошибочные данные (0 баллов). Технический сбой генерации JSON. Ознакомьтесь со стенограммой вручную.")
            
            with c1: draw_gauge_chart(sum(radar_data.values())/len(radar_data))
            with c2: draw_radar_chart(radar_data)
            
        st.markdown(analysis)
        st.divider()
        
        with st.expander("Сырые данные оценки (JSON)"):
            st.code(json.dumps(radar_data, indent=4, ensure_ascii=False), language='json')
        
        with st.expander("Стенограмма адаптивного интервью (Просмотр)"):
            messages = json.loads(hist_j)
            for msg in messages:
                if msg["role"] == "assistant": st.markdown(f"**Система:** {msg['content']}")
                elif "ПРОКТОРИНГ" in msg["content"]: st.error(f"**Кандидат:** {msg['content']}")
                else: st.info(f"**Кандидат:** {msg['content']}")
        
        transcript_text = "\n".join([f"{'Система' if m['role']=='assistant' else 'Кандидат'}: {m['content']}" for m in messages])
        radar_text = "\n".join([f"{k}: {v}" for k, v in radar_data.items()])
        download_content = f"ОТЧЕТ: {pos}\n\nНАРУШЕНИЯ ПРОКТОРИНГА: {cheat_count}\n\nОЦЕНКИ:\n{radar_text}\n\n{analysis}\n\nСТЕНОГРАММА:\n{transcript_text}"
        
        st.download_button("Скачать отчет PDF/TXT", download_content, file_name="hr_report.txt")

if __name__ == "__main__":
    main()
