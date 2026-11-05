commit 7217ea06dd4633967a4adf75da38b2640d124e9e
Author: TianXiang <tianxiang@users.noreply.github.com>
Date:   Wed Nov 4 23:43:27 2026 +0800

    docs: 官网开发者页新增 Skill 安装部署说明（一键脚本/手动/Agent 客户端）

diff --git a/frontend/src/pages/Developers.tsx b/frontend/src/pages/Developers.tsx
index 5b6ae5ae..50cb512f 100644
--- a/frontend/src/pages/Developers.tsx
+++ b/frontend/src/pages/Developers.tsx
@@ -76,6 +76,7 @@ interface CatalogResp {
 // ── 菜单 ──
 const MENU: SideMenuItem[] = [
   { id: 'quickstart', label: '快速开始', icon: <Rocket className="h-3.5 w-3.5" />, anchor: 'sec-quickstart' },
+  { id: 'install', label: 'Skill 安装', icon: <Terminal className="h-3.5 w-3.5" />, anchor: 'sec-install' },
   { id: 'auth', label: '鉴权方式', icon: <ShieldCheck className="h-3.5 w-3.5" />, anchor: 'sec-auth' },
   { id: 'ratelimit', label: '限流说明', icon: <Gauge className="h-3.5 w-3.5" />, anchor: 'sec-ratelimit' },
   { id: 'catalog', label: 'Skill 目录', icon: <ListTree className="h-3.5 w-3.5" />, anchor: 'sec-catalog' },
@@ -320,6 +321,106 @@ export default function DevelopersPage() {
         </div>
       </Section>
 
+      {/* ── Skill 安装 ── */}
+      <Section
+        id="sec-install"
+        title="Skill 安装"
+        description="把 SIDA Skill 装进本地环境或 Agent 客户端"
+        icon={<Terminal className="h-4 w-4" />}
+      >
+        <div className="space-y-4">
+          <InfoCard>
+            <div className="mb-2 text-[14px] font-medium text-foreground">方式一：一键安装脚本（推荐）</div>
+            <CodeBlock
+              code={`curl -sL ${base}/api/skills/install.sh | bash -s -- sk_你的Key`}
+              language="bash"
+            />
+            <p className="mt-2 text-[12px] leading-6 text-muted-foreground">
+              脚本会做三件事：把 <code className="font-mono">SIDA_API_KEY</code> 与{' '}
+              <code className="font-mono">SIDA_BASE_URL</code> 写入 <code className="font-mono">~/.bashrc</code> 与{' '}
+              <code className="font-mono">~/.zshrc</code>，然后立即调用 <code className="font-mono">/api/usage</code>{' '}
+              验证这把 Key 是否可用。验证失败会直接报错退出，不会留下「装了一半」的状态。
+              脚本本身不含任何密钥，Key 只作为参数传入。
+            </p>
+            <p className="mt-2 text-[12px] leading-6 text-muted-foreground">
+              重复执行是安全的：脚本会先移除旧的 <code className="font-mono">export SIDA_*</code> 行再写入，
+              所以换 Key 时直接重跑即可，不用手工编辑 rc 文件。
+            </p>
+          </InfoCard>
+
+          <InfoCard>
+            <div className="mb-2 text-[14px] font-medium text-foreground">方式二：手动配置（容器内 / 无交互 Shell）</div>
+            <CodeBlock
+              code={`export SIDA_API_KEY="sk_你的Key"
+export SIDA_BASE_URL="${base}"
+
+# 验证：返回当日用量与剩余额度即接通
+curl -s -H "X-API-Key: $SIDA_API_KEY" "$SIDA_BASE_URL/api/usage"`}
+              language="bash"
+            />
+            <p className="mt-2 text-[12px] leading-6 text-muted-foreground">
+              容器里不要依赖写 rc 文件——镜像重启就丢。改用 <code className="font-mono">docker run -e SIDA_API_KEY=...</code>{' '}
+              或在编排文件里注入环境变量。
+            </p>
+          </InfoCard>
+
+          <InfoCard>
+            <div className="mb-2 text-[14px] font-medium text-foreground">方式三：装进支持 Skill 的 Agent 客户端</div>
+            <p className="mb-2 text-[12px] leading-6 text-muted-foreground">
+              仓库里的 <code className="font-mono">skills/sida-market-data/</code> 是完整技能包：
+              <code className="font-mono">SKILL.md</code> 写明触发词、接口清单与数据口径，
+              <code className="font-mono">scripts/</code> 是仅用标准库的客户端（Python 3.9+ 可直接运行），
+              <code className="font-mono">references/</code> 是参数与排障手册。
+            </p>
+            <CodeBlock
+              code={`git clone https://github.com/xiaoze-hub/sida-pro.git
+# 把整个目录放进你所用客户端的 skills 目录（路径以该客户端文档为准）
+cp -r sida-pro/skills/sida-market-data ~/.hermes/skills/`}
+              language="bash"
+            />
+            <p className="mt-2 text-[12px] leading-6 text-muted-foreground">
+              不同客户端的 skills 目录约定不同（例如 Hermes 为 <code className="font-mono">~/.hermes/skills/</code>、
+              Claude Code 为 <code className="font-mono">~/.claude/skills/</code>），请以客户端自身文档为准。
+              装好后 Key 仍按方式一或二注入环境变量，技能包本身不存密钥。
+            </p>
+          </InfoCard>
+
+          <InfoCard>
+            <div className="mb-2 text-[14px] font-medium text-foreground">验证与卸载</div>
+            <CodeBlock
+              code={`# 1) Key 是否生效：返回 tier / used_today / remaining_today
+curl -s -H "X-API-Key: $SIDA_API_KEY" "${base}/api/usage"
+
+# 2) 当前档位可调用哪些接口
+curl -s -H "X-API-Key: $SIDA_API_KEY" "${base}/api/skills"
+
+# 卸载：删掉 rc 文件里的 SIDA 导出行（脚本写入的块以 "# SIDA Skill Gateway" 开头）
+grep -n "SIDA Skill Gateway" ~/.bashrc ~/.zshrc`}
+              language="bash"
+            />
+          </InfoCard>
+
+          <InfoCard>
+            <div className="mb-2 text-[14px] font-medium text-foreground">常见问题</div>
+            <ul className="space-y-2 text-[12px] leading-6 text-muted-foreground">
+              <li>
+                <span className="text-foreground">返回 401</span>：Key 不存在、已被冻结或已过期。到 API Key 页面确认状态与到期时间，
+                必要时重置一把新 Key 再重跑安装脚本。
+              </li>
+              <li>
+                <span className="text-foreground">交互终端正常、定时任务里读不到变量</span>：安装脚本只写 rc 文件，
+                cron / systemd 不会加载它。需要在任务里显式 <code className="font-mono">source ~/.bashrc</code>，
+                或在 unit 中用 <code className="font-mono">Environment=SIDA_API_KEY=...</code> 注入。
+              </li>
+              <li>
+                <span className="text-foreground">换机器后调用失败</span>：Key 与环境变量是两件事，新机器要重新注入；
+                仓库里的技能包可以复制，但密钥不要随代码一起提交。
+              </li>
+            </ul>
+          </InfoCard>
+        </div>
+      </Section>
+
       {/* ── 鉴权方式 ── */}
       <Section id="sec-auth" title="鉴权方式" description="三种方式调用 Skill API" icon={<ShieldCheck className="h-4 w-4" />}>
         <div className="space-y-3">
