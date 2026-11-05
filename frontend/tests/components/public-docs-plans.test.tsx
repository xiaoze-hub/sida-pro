commit 7217ea06dd4633967a4adf75da38b2640d124e9e
Author: TianXiang <tianxiang@users.noreply.github.com>
Date:   Wed Nov 4 23:43:27 2026 +0800

    docs: 官网开发者页新增 Skill 安装部署说明（一键脚本/手动/Agent 客户端）

diff --git a/frontend/tests/components/public-docs-plans.test.tsx b/frontend/tests/components/public-docs-plans.test.tsx
index b090af28..8da39c0f 100644
--- a/frontend/tests/components/public-docs-plans.test.tsx
+++ b/frontend/tests/components/public-docs-plans.test.tsx
@@ -112,4 +112,27 @@ describe('公开开发者文档', () => {
     expect(await screen.findByRole('status')).toHaveProperty('textContent', '复制未完成，请选择代码后手动复制。')
     expect(screen.queryByText('已复制')).toBeNull()
   })
+
+  it('Skill 安装分类给出真实安装方式: 一键脚本、手动变量、技能包目录与验证命令', async () => {
+    show(<Developers />, '/developers?section=sec-install')
+    expect(screen.getByRole('heading', { name: 'Skill 安装' })).toBeTruthy()
+
+    // 一键脚本：必须指向真实存在的安装端点，而不是文档里编的路径
+    expect(document.body.textContent).toContain('/api/skills/install.sh')
+    expect(document.body.textContent).toContain('bash -s -- sk_你的Key')
+
+    // 手动方式：两个环境变量名必须与安装脚本写入的一致
+    expect(document.body.textContent).toContain('SIDA_API_KEY')
+    expect(document.body.textContent).toContain('SIDA_BASE_URL')
+
+    // 技能包：目录名必须与仓库实际存在的包一致
+    expect(document.body.textContent).toContain('skills/sida-market-data')
+
+    // 验证：给出可自查的两个真实端点
+    expect(document.body.textContent).toContain('/api/usage')
+    expect(document.body.textContent).toContain('/api/skills')
+
+    // 公开面不许出现管理员端点路径（这是本页的历史事故）
+    expect(document.body.textContent).not.toContain('/api/admin/')
+  })
 })
