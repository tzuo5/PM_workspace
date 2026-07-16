from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from services.llm_console import logged_llm_call


class LlmConsoleTests(unittest.TestCase):
    def test_prompt_and_response_are_printed(self) -> None:
        output = io.StringIO()

        def fake_call(config, messages):
            self.assertEqual(config["model"], "test-model")
            self.assertEqual(messages[0]["content"], "system prompt")
            return '{"ok": true}'

        with redirect_stdout(output):
            response = logged_llm_call(
                fake_call,
                {"provider": "test", "model": "test-model", "base_url": "http://localhost"},
                [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "user prompt"},
                ],
            )

        text = output.getvalue()
        self.assertEqual(response, '{"ok": true}')
        self.assertIn("LLM CONSOLE | REQUEST", text)
        self.assertIn("PROMPT 1 / SYSTEM", text)
        self.assertIn("system prompt", text)
        self.assertIn("user prompt", text)
        self.assertIn("LLM CONSOLE | RESPONSE", text)
        self.assertIn('{"ok": true}', text)

    def test_errors_are_printed_and_reraised(self) -> None:
        output = io.StringIO()

        def fake_call(config, messages):
            raise RuntimeError("network down")

        with self.assertRaisesRegex(RuntimeError, "network down"):
            with redirect_stdout(output):
                logged_llm_call(fake_call, {}, [])

        self.assertIn("LLM CONSOLE | ERROR", output.getvalue())
        self.assertIn("network down", output.getvalue())


if __name__ == "__main__":
    unittest.main()
