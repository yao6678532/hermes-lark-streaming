# Native installation — hermes-lark-streaming

This local migration uses Hermes v0.23.1, pinned to
`3b421578588243f920886e6fe79a6faa87ea026e`. Earlier Hermes needs the retained
legacy plugin and environment; the new runtime does not inject hooks.

## Install

Use Hermes's actual PM-managed Python as `$HERMES_PYTHON`. Back up the old
plugin, core revision, environment, profile configs/state and service definitions
before migrating. Stop shared gateways only after active work has completed.

For an existing injected installation, run the retained old plugin's `uninstall`
(or the new CLI's `legacy-clean`) using the old environment before upgrading
Hermes. Verify the six cleaned source files equal the old Git baseline. Cleanup
removes plugin markers; never copy historical whole-file backups over core files.

Install the reviewed committed local tree through the official manager:

```bash
hermes plugins install file:///absolute/path/hermes-lark-streaming --enable --yes-deps
```

The official scanner reports caution for retained AST migration helpers and test
fixtures. Review the report; `--force` is the manager's explicit acceptance of
that known caution. No dangerous verdict is bypassed. Local Git installation
uses committed content, so commit the reviewed changes first.

Keep existing credentials and `streaming` / `display.platforms.feishu` settings.
Enable the plugin separately in every Feishu profile. For each profile, run:

```bash
$HERMES_PYTHON -m hermes_lark_streaming migrate-config
$HERMES_PYTHON -m hermes_lark_streaming migrate-config --apply
$HERMES_PYTHON -m hermes_lark_streaming verify
```

Select a profile using Hermes's `HERMES_HOME` / `--profile` mechanism. Migration
prints only changed plugin/transport fields and is idempotent. It preserves the
old visual defaults, adds required native switches and preserves explicit tuning.

Restart gateways using their actual supervisor and PM environment. Then run
`status`: the registered factory must be `FeishuStreamingAdapter`, no legacy
injections may remain, and the live gateway must report the native adapter.
Check Feishu connection health and startup logs. `verify` alone checks interface
compatibility; a stopped gateway cannot prove live platform connection.

## Disable, remove, update

```bash
hermes plugins disable hermes-lark-streaming
hermes plugins remove hermes-lark-streaming
```

Disabling restores the built-in Feishu adapter after restart. It does not delete
bot credentials, sessions or Cron destinations. Update with the plugin manager
from a reviewed commit; rerun the fixed-host contract tests before host upgrades.

For rollback, stop new gateways, restore the recorded old core/plugin/environment
and matching configuration, reinstall old marked hooks with the retained old
plugin, restore service definitions, and restart the original five gateways.
See the machine-specific backup directory for exact paths and revisions.
