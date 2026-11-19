# Pushary for Hermes

Ask a question in Hermes and answer it on your phone. Try it with your own device before routing command approvals there.

## Connect your phone

1. [Set up your Pushary Agent account](https://pushary.com/sign-up?from=ai-coding&utm_source=hermes-plugin&utm_medium=quickstart&utm_campaign=first-question). Pair your phone with that account and get its API key from Agent settings.
2. In your terminal, install the catalog plugin:

   ```sh
   hermes plugins install pushary
   ```

3. Enter `PUSHARY_API_KEY` when Hermes prompts for it. If you missed the prompt, save the key in the plugin’s settings in Hermes Desktop. Keep it out of chat. Start a new Hermes session (for a running gateway, use `hermes gateway restart`).

## Try one question

Tell Hermes: **Use `pushary_ask` to ask me “Did this test reach your phone?” Report my answer and do nothing else.** Open the question on your paired phone and tap **Yes** or **No**. Hermes should report the answer you tapped.

If nothing arrives, check the account and notification permissions on your phone. A pending question is still live: wait for the same question rather than sending another. If Pushary hands it back to Hermes, answer the local prompt instead. No answer is not approval.

Command approvals are separate from this test. To route them to your phone, set `security.approval.transport` to `pushary`. Set `security.approval.transport_fallback` to `builtin` if you want Hermes’s local prompt after a safe handoff; an unconfirmed withdrawal does not authorize the command. See the [plugin guide](https://github.com/Pushary/hermes-plugin-pushary#readme) for approval routing and what the plugin reads and sends.
