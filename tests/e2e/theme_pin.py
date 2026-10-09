"""把无头浏览器看到的 `prefers-color-scheme` 钉成 **light**（/tmp/theme_pin.py）。

为什么需要：Folia 的主题跟着系统偏好走，而**宿主机的外观是会变的** ——
macOS 到点自动转深色。e2e17~22 这几套是深色模式之前写的，断言的是浅色下的
样式（比如「四种标注都走 multiply 混色」）；宿主一入夜，`theme-dark` 被挂上，
`--hl-*` 与混色模式全换一套，断言就凭空红了。

钉死它，这几套用例才与"跑的时候是白天还是晚上"无关。
深色那一支由 e2e23 专门覆盖 —— 那边反过来把偏好钉成 dark。

插在**主题脚本之前**：index.html 里那段同步脚本在 `<head>` 里就执行，
晚于它插桩就没用了。
"""

ANCHOR = "    <script>\n      (function () {\n        try {\n          var mq = window.matchMedia"

PIN = """    <script>
      (function () {
        var real = window.matchMedia.bind(window);
        window.matchMedia = function (query) {
          if (/prefers-color-scheme:\\s*dark/.test(query)) {
            return {
              matches: false,
              media: query,
              onchange: null,
              addEventListener: function () {},
              removeEventListener: function () {},
              addListener: function () {},
              removeListener: function () {},
            };
          }
          return real(query);
        };
      })();
    </script>
"""


def pin_light(index: str) -> str:
    """在主题脚本前插入"永远报浅色"的 matchMedia 桩。

    找不到锚点就**报错**而不是静默放过 —— 静默的话，哪天主题脚本改名/挪位置，
    这套用例会悄悄回到"看天吃饭"的状态，红不红全凭跑的时候是白天还是晚上。
    """
    assert ANCHOR in index, "没找到主题脚本，钉浅色的插桩位置失效了"
    return index.replace(ANCHOR, PIN + ANCHOR, 1)
