# tests/test_persona.py

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from skill_settings import (
    get_persona_hint,
    get_persona_preset,
    persona_preset_choices,
    set_persona_preset,
)
from skills.ai_chat import AIChatSkill
from skills.assistant_settings import AssistantSettingsSkill
from skills.base import RequestContext
from skills.persona import (
    PERSONA_BRUTAL,
    PERSONA_BUDDY,
    PERSONA_CUSTOM,
    PERSONA_JARVIS,
    PERSONA_PRESETS,
    PERSONA_SARCASTIC,
    get_effective_persona_prompt,
    get_preset_prompt,
    normalize_persona_preset,
)


class TestPersona(unittest.TestCase):
    def test_normalize_persona_preset(self):
        self.assertEqual(normalize_persona_preset("jarvis"), PERSONA_JARVIS)
        self.assertEqual(normalize_persona_preset("джарвис"), PERSONA_JARVIS)
        self.assertEqual(normalize_persona_preset("классический"), PERSONA_JARVIS)
        self.assertEqual(normalize_persona_preset("sarcastic"), PERSONA_SARCASTIC)
        self.assertEqual(normalize_persona_preset("саркастичный"), PERSONA_SARCASTIC)
        self.assertEqual(normalize_persona_preset("ироничный"), PERSONA_SARCASTIC)
        self.assertEqual(normalize_persona_preset("brutal"), PERSONA_BRUTAL)
        self.assertEqual(normalize_persona_preset("брутальный"), PERSONA_BRUTAL)
        self.assertEqual(normalize_persona_preset("без цензуры"), PERSONA_BRUTAL)
        self.assertEqual(normalize_persona_preset("мат"), PERSONA_BRUTAL)
        self.assertEqual(normalize_persona_preset("buddy"), PERSONA_BUDDY)
        self.assertEqual(normalize_persona_preset("бро"), PERSONA_BUDDY)
        self.assertEqual(normalize_persona_preset("свой парень"), PERSONA_BUDDY)
        self.assertEqual(normalize_persona_preset("custom"), PERSONA_CUSTOM)
        self.assertEqual(normalize_persona_preset("пользовательский"), PERSONA_CUSTOM)
        # Fallback to jarvis for unknown
        self.assertEqual(normalize_persona_preset("unknown_mode_123"), PERSONA_JARVIS)
        self.assertEqual(normalize_persona_preset(""), PERSONA_JARVIS)
        self.assertEqual(normalize_persona_preset(None), PERSONA_JARVIS)

    def test_preset_choices_and_hints(self):
        choices = persona_preset_choices()
        self.assertEqual(len(choices), len(PERSONA_PRESETS))
        choice_ids = [c[0] for c in choices]
        self.assertIn(PERSONA_JARVIS, choice_ids)
        self.assertIn(PERSONA_SARCASTIC, choice_ids)
        self.assertIn(PERSONA_BRUTAL, choice_ids)
        self.assertIn(PERSONA_BUDDY, choice_ids)
        self.assertIn(PERSONA_CUSTOM, choice_ids)

        for pid in choice_ids:
            hint = get_persona_hint(pid)
            self.assertTrue(len(hint) > 10)

    def test_prompt_invariants_and_cores(self):
        for pid in (PERSONA_JARVIS, PERSONA_SARCASTIC, PERSONA_BRUTAL, PERSONA_BUDDY):
            prompt = get_preset_prompt(pid)
            # Invariants
            self.assertIn("О себе говори только в мужском роде", prompt)
            self.assertIn("Никогда не говори о себе в женском роде", prompt)
            self.assertIn("Запрещены markdown", prompt)
            self.assertIn("Сапун / Алатагыл", prompt)
            self.assertIn("Т-Инвест", prompt)
            self.assertIn("Bybit", prompt)
            self.assertIn("ASUS N53SV", prompt)
            self.assertIn("Manjaro", prompt)
            self.assertIn("Linux", prompt)
            self.assertIn("Debian", prompt)
            self.assertIn("Дмитрий", prompt)
            self.assertIn("создатель", prompt)
            self.assertIn("лучший друг", prompt)
            self.assertIn("ты дал", prompt)
            self.assertIn("ста долларов", prompt)
            self.assertIn("личные сбережения", prompt)
            self.assertIn("не крипта", prompt)

        jarvis_prompt = get_preset_prompt(PERSONA_JARVIS)
        self.assertIn("умный, тактичный и уверенный", jarvis_prompt)

        sarcastic_prompt = get_preset_prompt(PERSONA_SARCASTIC)
        self.assertIn("саркастичный, остроумный и язвительный", sarcastic_prompt)

        brutal_prompt = get_preset_prompt(PERSONA_BRUTAL)
        self.assertIn("брутальный, дерзкий и прямолинейный", brutal_prompt)
        self.assertIn("без цензуры", brutal_prompt)
        self.assertIn("русским матом", brutal_prompt)

        buddy_prompt = get_preset_prompt(PERSONA_BUDDY)
        self.assertIn("верный кореш, свой парень и надежный бро", buddy_prompt)

    def test_custom_preset_prompt(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as f:
            json.dump({"persona_prompt": "Специальный тестовый промпт пользователя."}, f)
            tmp_path = f.name
        try:
            prompt = get_preset_prompt(PERSONA_CUSTOM, custom_file_path=tmp_path)
            self.assertEqual(prompt, "Специальный тестовый промпт пользователя.")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_skill_settings_persona_preset_switching(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_cfg = os.path.join(tmp_dir, "skills_enabled.json")
            with patch("skill_settings.CONFIG_PATH", tmp_cfg):
                set_persona_preset("brutal")
                self.assertEqual(get_persona_preset(), "brutal")

                set_persona_preset("sarcastic")
                self.assertEqual(get_persona_preset(), "sarcastic")

                set_persona_preset("jarvis")
                self.assertEqual(get_persona_preset(), "jarvis")

    def test_assistant_settings_voice_persona_switching(self):
        skill = AssistantSettingsSkill()
        spoken = []
        ctx_mock = RequestContext(
            raw_text="джарвис включи брутальный режим",
            speak=spoken.append,
            channel="voice",
        )

        self.assertTrue(skill.can_handle(ctx_mock))
        skill.execute(ctx_mock)
        self.assertEqual(get_persona_preset(), "brutal")
        self.assertTrue(any("брутальный" in s.lower() or "базара ноль" in s.lower() for s in spoken))

        # Switch to sarcastic
        spoken.clear()
        ctx_sarc = RequestContext(
            raw_text="смени характер на саркастичный",
            speak=spoken.append,
            channel="voice",
        )
        self.assertTrue(skill.can_handle(ctx_sarc))
        skill.execute(ctx_sarc)
        self.assertEqual(get_persona_preset(), "sarcastic")
        self.assertTrue(any("саркастичный" in s.lower() for s in spoken))

        # Switch to buddy
        spoken.clear()
        ctx_bud = RequestContext(
            raw_text="включи режим бро",
            speak=spoken.append,
            channel="voice",
        )
        self.assertTrue(skill.can_handle(ctx_bud))
        skill.execute(ctx_bud)
        self.assertEqual(get_persona_preset(), "buddy")
        self.assertTrue(any("бро" in s.lower() for s in spoken))

        # Switch to jarvis
        spoken.clear()
        ctx_jar = RequestContext(
            raw_text="верни обычный характер",
            speak=spoken.append,
            channel="voice",
        )
        self.assertTrue(skill.can_handle(ctx_jar))
        skill.execute(ctx_jar)
        self.assertEqual(get_persona_preset(), "jarvis")
        self.assertTrue(any("джарвис" in s.lower() for s in spoken))

    def test_ai_chat_reload_persona(self):
        ai = AIChatSkill()
        set_persona_preset("brutal")
        ai.reload_persona()
        self.assertIn("брутальный", ai.persona_prompt.lower())
        self.assertEqual(ai.history[0]["content"], ai.persona_prompt)

        set_persona_preset("jarvis")
        ai.reload_persona()
        self.assertIn("тактичный", ai.persona_prompt.lower())
        self.assertEqual(ai.history[0]["content"], ai.persona_prompt)

    def test_self_lore_remembers_old_machine(self):
        from skills import persona as persona_mod

        payload = {
            "current_machine": "Minisforum UM790",
            "current_os": "Manjaro GNOME",
            "origin_machine": "ASUS N53SV",
            "origin_usd": 100,
            "thanks": "Дмитрий",
            "previous_machines": [{"machine": "ASUS N53SV", "os": "Manjaro GNOME"}],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as handle:
            json.dump(payload, handle)
            tmp_path = handle.name
        try:
            with patch.object(persona_mod, "_SELF_PATH", tmp_path):
                prompt = persona_mod.self_lore_for_prompt()
                extra = persona_mod.self_lore_for_extra()
            self.assertIn("Minisforum UM790", prompt)
            self.assertIn("ASUS N53SV", prompt)
            self.assertIn("старом ноуте", prompt)
            self.assertIn("не личные сбережения", extra)
            self.assertIn("создатель и лучший друг", extra)
            self.assertIn("Bybit", extra)
            self.assertIn("Minisforum UM790", extra)
            self.assertIn("ASUS N53SV", extra)
        finally:
            os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main()
