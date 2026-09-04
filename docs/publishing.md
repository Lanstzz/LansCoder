# 发布检查(`LansCoder` + `lanscoder-core`)

> 单 tag 双 dist:一个 tag 同时构建并发布 `LansCoder`(TUI 薄壳)与 `lanscoder-core`(headless SDK)。
> 版本单一事实来源 `lanscoder/core/_version.py`;root 薄壳版本与 `lanscoder-core==` pin 硬编码一致,
> 由 `tests/test_dist_metadata.py`(本地)与 `publish-pypi.yml` 的 tag 校验(发布期)强制,漂移即红。

## 0. 发布前门禁(本地)

```sh
python -m pytest
python -m ruff check .
```

- 版本一致性:`tests/test_dist_metadata.py` 锁定 root `pyproject.toml` version == core `_version.py`,
  root pin `lanscoder-core[llm,mcp]==<version>` 与版本一致。
- `CHANGELOG.md` 已更新(keep-a-changelog,当前版本移入 Released 区)。

## 1. 本地构建 + 元数据校验

```sh
python -m build                                   # root → dist/lanscoder-<v>-*.whl + .tar.gz
python -m build --wheel packages/lanscoder-core   # → packages/lanscoder-core/dist/lanscoder_core-<v>-*.whl
python -m twine check dist/* packages/lanscoder-core/dist/*
```

- 两个 wheel 版本必须都等于 `_version.py`;`twine check` 全 PASSED(不一致先修,CI tag 校验也会拦)。

## 2. Test PyPI 演练(推荐,真实上传前)

需要 Test PyPI 账号与 API token(<https://test.pypi.org/manage/account/token/>):

```sh
python -m twine upload --repository testpypi dist/*
python -m twine upload --repository testpypi packages/lanscoder-core/dist/*
```

干净 venv 验证:

```sh
python -m venv /tmp/verify
/tmp/verify/bin/python -m pip install -i https://test.pypi.org/simple/ lanscoder-core
/tmp/verify/bin/python -c "from lanscoder.core import create_agent_session; print(create_agent_session.__name__)"
/tmp/verify/bin/python -m pip show lanscoder-core    # Requires: anyio, portalocker, PyYAML(无 textual)
# 薄壳:再装 LansCoder 应自动解析 lanscoder-core[llm,mcp] + TUI 依赖;卸载任一方不影响另一方
```

## 3. 真实发布(tag push 触发 CI)

1. 确认版本已 bump:`lanscoder/core/_version.py`、root `pyproject.toml` version、root pin、`CHANGELOG.md`。
2. 打 tag 并推送:

   ```sh
   git tag v<X.Y.Z>
   git push origin v<X.Y.Z>
   ```

3. 观察 `Publish to PyPI` workflow:test → minimal-core-deps → publish
   (双 build + `twine check` + 上传 `dist/*` 与 `packages/lanscoder-core/dist/*`)。
4. 发布后验证:

   ```sh
   pip install lanscoder-core       # SDK 用户:from lanscoder.core import ... 即用
   pipx install lanscoder           # TUI 用户
   lanscoder --help
   ```

## 4. 回滚/修正

- PyPI 不允许同版本重复上传;线上问题通过 bump patch 发布新版本修复。
- 版本漂移:任一版本来源不一致,本地 `test_dist_metadata.py` 与 CI tag 校验都会红。
