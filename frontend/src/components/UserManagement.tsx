import { useCallback, useEffect, useState } from 'react'
import { Users, Plus, Trash2, KeyRound, Ban, CheckCircle2, UserCog, Boxes, ShieldCheck, Loader2 } from 'lucide-react'
import { authApi, fetchAPI, UserInfo } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@panwatch/base-ui/components/ui/dialog'

interface Props {
  currentUser: UserInfo | null
}

type ModelAccessMode = 'inherit' | 'granted' | 'deny_all'

interface PermissionItem {
  key: string
  label: string
  group: string
}

interface ModelInfo {
  id: number
  name: string
  model: string
  service_name: string | null
}

interface ModelAccessData {
  mode: ModelAccessMode
  model_ids: number[]
  all_models: ModelInfo[]
  user_role: string
  username: string
}

const MODE_OPTIONS: { value: ModelAccessMode; label: string; hint: string }[] = [
  { value: 'inherit', label: '继承全部平台模型', hint: '未单独授权时, 可使用平台当前全部模型' },
  { value: 'granted', label: '仅使用勾选模型', hint: '只能使用下方勾选的模型(不勾选=全部禁用)' },
  { value: 'deny_all', label: '全部禁用', hint: '不可使用任何 AI 模型' },
]

export default function UserManagement({ currentUser }: Props) {
  const { toast } = useToast()
  const [users, setUsers] = useState<UserInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newPass, setNewPass] = useState('')
  const [newRole, setNewRole] = useState<'owner' | 'member' | 'guest'>('member')
  // 改密对话框
  const [resetTarget, setResetTarget] = useState<UserInfo | null>(null)
  const [resetPass, setResetPass] = useState('')
  // 模型授权对话框
  const [accessTarget, setAccessTarget] = useState<UserInfo | null>(null)
  const [accessLoading, setAccessLoading] = useState(false)
  const [accessSaving, setAccessSaving] = useState(false)
  const [accessMode, setAccessMode] = useState<ModelAccessMode>('inherit')
  const [accessModels, setAccessModels] = useState<ModelInfo[]>([])
  const [checkedIds, setCheckedIds] = useState<number[]>([])
  // 模块权限对话框
  const [permTarget, setPermTarget] = useState<UserInfo | null>(null)
  const [permLoading, setPermLoading] = useState(false)
  const [permSaving, setPermSaving] = useState(false)
  const [permAll, setPermAll] = useState<PermissionItem[]>([])
  const [permGranted, setPermGranted] = useState<string[]>([])
  const [permDefaults, setPermDefaults] = useState<string[]>([])

  const isOwner = currentUser?.role === 'owner'

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await authApi.listUsers()
      setUsers(data.users || [])
    } catch {
      /* 非 owner 无权限, 静默 */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (isOwner) void load()
  }, [isOwner, load])

  const handleCreate = async () => {
    if (newName.trim().length < 2) return toast('用户名至少 2 位', 'error')
    if (newPass.length < 8) return toast('密码至少 8 位', 'error')
    try {
      await authApi.createUser({ username: newName.trim(), password: newPass, role: newRole })
      toast('子账号创建成功', 'success')
      setShowCreate(false)
      setNewName('')
      setNewPass('')
      void load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '创建失败', 'error')
    }
  }

  const handleToggleActive = async (u: UserInfo) => {
    try {
      await authApi.updateUser(u.id, { is_active: !u.is_active })
      toast(u.is_active ? `已禁用 ${u.username}` : `已启用 ${u.username}`, 'success')
      void load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    }
  }

  const handleResetPassword = async () => {
    if (!resetTarget) return
    if (resetPass.length < 8) return toast('密码至少 8 位', 'error')
    try {
      await authApi.updateUser(resetTarget.id, { password: resetPass })
      toast(`${resetTarget.username} 密码已重置`, 'success')
      setResetTarget(null)
      setResetPass('')
    } catch (e) {
      toast(e instanceof Error ? e.message : '重置失败', 'error')
    }
  }

  const handleDelete = async (u: UserInfo) => {
    if (!window.confirm(`确定删除用户 ${u.username}?其持仓/自选/渠道将一并删除`)) return
    try {
      await authApi.deleteUser(u.id)
      toast(`已删除 ${u.username}`, 'success')
      void load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error')
    }
  }

  // ── 模块权限 ────────────────────────────────────────────────────────
  const openPermissionAccess = async (u: UserInfo) => {
    setPermTarget(u)
    setPermLoading(true)
    setPermSaving(false)
    try {
      const data = await fetchAPI<{ granted: string[]; role_defaults: string[]; all_permissions: PermissionItem[] }>(`/users/${u.id}/permissions`, { cacheMode: 'reload' })
      setPermAll(data.all_permissions || [])
      setPermGranted(data.granted || [])
      setPermDefaults(data.role_defaults || [])
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载模块权限失败', 'error')
      setPermTarget(null)
    } finally {
      setPermLoading(false)
    }
  }

  const togglePerm = (key: string) => {
    setPermGranted(prev => (prev.includes(key) ? prev.filter(x => x !== key) : [...prev, key]))
  }

  const savePermissions = async () => {
    if (!permTarget) return
    setPermSaving(true)
    try {
      await fetchAPI(`/users/${permTarget.id}/permissions`, {
        method: 'PUT',
        body: JSON.stringify({ permissions: permGranted }),
      })
      toast(`${permTarget.username} 的模块权限已保存`, 'success')
      setPermTarget(null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存模块权限失败', 'error')
    } finally {
      setPermSaving(false)
    }
  }

  // ── 模型授权 ────────────────────────────────────────────────────────
  const openModelAccess = async (u: UserInfo) => {
    setAccessTarget(u)
    setAccessLoading(true)
    setAccessSaving(false)
    try {
      const data = await fetchAPI<ModelAccessData>(`/users/${u.id}/model-access`, { cacheMode: 'reload' })
      setAccessMode(data.mode)
      setAccessModels(data.all_models || [])
      setCheckedIds(data.model_ids || [])
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载模型授权失败', 'error')
      setAccessTarget(null)
    } finally {
      setAccessLoading(false)
    }
  }

  const toggleModel = (id: number) => {
    setCheckedIds(prev => (prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]))
  }

  const saveModelAccess = async () => {
    if (!accessTarget) return
    setAccessSaving(true)
    try {
      await fetchAPI(`/users/${accessTarget.id}/model-access`, {
        method: 'PUT',
        body: JSON.stringify({
          mode: accessMode,
          ...(accessMode === 'granted' ? { model_ids: checkedIds } : {}),
        }),
      })
      toast(`${accessTarget.username} 的模型授权已保存`, 'success')
      setAccessTarget(null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存失败', 'error')
    } finally {
      setAccessSaving(false)
    }
  }

  if (!isOwner) {
    return (
      <div className="rounded-xl border border-border/50 bg-card p-6 text-center text-[13px] text-muted-foreground">
        <UserCog className="mx-auto mb-2 h-6 w-6 opacity-50" />
        仅管理员可管理用户
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-border/50 bg-card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-[13px] font-semibold">
          <Users className="h-4 w-4 text-primary" /> 用户管理({users.length})
        </h3>
        <Button size="sm" variant="outline" onClick={() => setShowCreate(v => !v)}>
          <Plus className="mr-1 h-3.5 w-3.5" /> 新建子账号
        </Button>
      </div>

      {showCreate && (
        <div className="mb-3 space-y-2 rounded-lg border border-border/40 bg-accent/20 p-3">
          <input
            className="w-full rounded-md border border-border/60 bg-background px-2.5 py-1.5 text-[12px]"
            placeholder="用户名(如: 小李)"
            value={newName}
            onChange={e => setNewName(e.target.value)}
          />
          <input
            className="w-full rounded-md border border-border/60 bg-background px-2.5 py-1.5 text-[12px]"
            placeholder="密码(至少8位)"
            type="password"
            value={newPass}
            onChange={e => setNewPass(e.target.value)}
          />
          <div className="flex flex-wrap items-center gap-3 text-[12px]">
            <label className="flex items-center gap-1">
              <input type="radio" checked={newRole === 'member'} onChange={() => setNewRole('member')} /> 普通成员
            </label>
            <label className="flex items-center gap-1">
              <input type="radio" checked={newRole === 'guest'} onChange={() => setNewRole('guest')} /> 访客(只读)
            </label>
            <label className="flex items-center gap-1">
              <input type="radio" checked={newRole === 'owner'} onChange={() => setNewRole('owner')} /> 管理员
            </label>
            <div className="flex-1" />
            <Button size="sm" variant="ghost" onClick={() => { setShowCreate(false); setNewName(''); setNewPass(''); setNewRole('member') }}>取消</Button>
            <Button size="sm" onClick={handleCreate}>创建</Button>
          </div>
        </div>
      )}

      <div className="divide-y divide-border/40">
        {users.map(u => (
          <div key={u.id} className="flex items-center justify-between py-2 text-[12px]">
            <div className="flex items-center gap-2">
              <span className={`inline-block h-2 w-2 rounded-full ${u.is_active ? 'bg-emerald-500' : 'bg-muted'}`} />
              <span className="font-medium">{u.username}</span>
              <span className={`rounded px-1.5 py-0.5 text-[10px] ${u.role === 'owner' ? 'bg-amber-500/15 text-amber-600' : u.role === 'guest' ? 'bg-violet-500/15 text-violet-600' : 'bg-sky-500/15 text-sky-600'}`}>
                {u.role === 'owner' ? '管理员' : u.role === 'guest' ? '访客' : '成员'}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              {u.id !== currentUser?.id && (
                <>
                <button
                  className="flex items-center gap-1 rounded border border-border/50 px-1.5 py-1 text-[11px] text-muted-foreground hover:border-primary/30 hover:text-primary"
                  title="配置该用户可用的 AI 模型"
                  aria-label={`配置 ${u.username} 可用的 AI 模型`}
                  onClick={() => void openModelAccess(u)}
                >
                  <Boxes className="h-3 w-3" /> 模型授权
                </button>
                <button
                  className="flex items-center gap-1 rounded border border-border/50 px-1.5 py-1 text-[11px] text-muted-foreground hover:border-primary/30 hover:text-primary"
                  title="配置该用户可用的功能模块"
                  aria-label={`配置 ${u.username} 可用的功能模块`}
                  onClick={() => void openPermissionAccess(u)}
                >
                  <ShieldCheck className="h-3 w-3" /> 模块权限
                </button>
                </>
              )}
              {u.role !== 'owner' && (
                <>
                  <button
                    className="rounded p-1.5 text-muted-foreground hover:bg-accent"
                    title="重置密码"
                    aria-label={`重置 ${u.username} 的密码`}
                    onClick={() => setResetTarget(u)}
                  >
                    <KeyRound className="h-3.5 w-3.5" />
                  </button>
                  <button
                    className="rounded p-1.5 text-muted-foreground hover:bg-accent"
                    title={u.is_active ? '禁用' : '启用'}
                    aria-label={u.is_active ? `禁用 ${u.username}` : `启用 ${u.username}`}
                    onClick={() => handleToggleActive(u)}
                  >
                    {u.is_active ? <Ban className="h-3.5 w-3.5" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                  </button>
                  <button
                    className="rounded p-1.5 text-rose-500/70 hover:bg-rose-500/10"
                    title="删除"
                    aria-label={`删除 ${u.username}`}
                    onClick={() => handleDelete(u)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </>
              )}
            </div>
          </div>
        ))}
        {!users.length && !loading && (
          <div className="py-4 text-center text-[12px] text-muted-foreground">暂无用户</div>
        )}
      </div>

      {resetTarget && (
        <div className="mt-3 rounded-lg border border-border/40 bg-accent/20 p-3">
          <div className="mb-2 text-[12px] font-medium">重置 {resetTarget.username} 的密码</div>
          <div className="flex gap-2">
            <input
              className="flex-1 rounded-md border border-border/60 bg-background px-2.5 py-1.5 text-[12px]"
              placeholder="新密码(至少8位)"
              type="password"
              value={resetPass}
              onChange={e => setResetPass(e.target.value)}
            />
            <Button size="sm" onClick={handleResetPassword}>确定</Button>
            <Button size="sm" variant="outline" onClick={() => setResetTarget(null)}>取消</Button>
          </div>
        </div>
      )}

      <Dialog open={!!accessTarget} onOpenChange={(open) => { if (!open) setAccessTarget(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>模型授权</DialogTitle>
            <DialogDescription>
              {accessTarget ? `配置 ${accessTarget.username} 可使用的 AI 模型` : ' '}
            </DialogDescription>
          </DialogHeader>

          {accessLoading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-[12px] text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 加载中
            </div>
          ) : (
            <div className="space-y-4">
              {/* 模式三选 */}
              <div className="space-y-2">
                {MODE_OPTIONS.map(opt => (
                  <label
                    key={opt.value}
                    className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-border/50 bg-background/50 px-3 py-2.5 transition-colors hover:border-primary/30"
                  >
                    <input
                      type="radio"
                      name="model-access-mode"
                      className="mt-0.5 h-3.5 w-3.5 accent-primary"
                      checked={accessMode === opt.value}
                      onChange={() => setAccessMode(opt.value)}
                    />
                    <span className="min-w-0">
                      <span className="block text-[12px] font-medium text-foreground">{opt.label}</span>
                      <span className="block text-[11px] text-muted-foreground">{opt.hint}</span>
                    </span>
                  </label>
                ))}
              </div>

              {/* 仅勾选模式下: 模型 checkbox 列表 */}
              {accessMode === 'granted' && (
                <div>
                  <div className="mb-1.5 flex items-center justify-between text-[11px] text-muted-foreground">
                    <span>可选模型({accessModels.length})</span>
                    <button
                      className="text-[11px] text-primary hover:underline"
                      onClick={() => setCheckedIds(accessModels.map(m => m.id))}
                    >
                      全选
                    </button>
                  </div>
                  <div className="max-h-52 space-y-1 overflow-y-auto rounded-lg border border-border/40 bg-background/40 p-2">
                    {accessModels.length === 0 && (
                      <div className="py-4 text-center text-[12px] text-muted-foreground">暂无可用模型</div>
                    )}
                    {accessModels.map(m => (
                      <label
                        key={m.id}
                        className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 transition-colors hover:bg-accent"
                      >
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 accent-primary"
                          checked={checkedIds.includes(m.id)}
                          onChange={() => toggleModel(m.id)}
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-[12px] font-medium text-foreground">{m.name}</span>
                          <span className="block truncate font-mono text-[10px] text-muted-foreground">
                            {m.model}{m.service_name ? ` · ${m.service_name}` : ''}
                          </span>
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              )}

              {/* 操作 */}
              <div className="flex justify-end gap-2 border-t border-border/40 pt-3">
                <Button size="sm" variant="outline" onClick={() => setAccessTarget(null)}>取消</Button>
                <Button size="sm" onClick={() => void saveModelAccess()} disabled={accessSaving}>
                  {accessSaving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                  {accessSaving ? '保存中' : '保存'}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* 模块权限 Dialog */}
      <Dialog open={!!permTarget} onOpenChange={(open) => { if (!open) setPermTarget(null) }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>模块权限</DialogTitle>
            <DialogDescription>
              {permTarget ? `配置 ${permTarget.username} 可用的功能模块(勾选 = 在角色基础上追加授权,管理类模块默认不开放)` : ''}
            </DialogDescription>
          </DialogHeader>
          {permLoading ? (
            <div className="py-6 flex items-center justify-center text-[12px] text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin mr-2" /> 加载中…
            </div>
          ) : (
            <div className="space-y-4 max-h-[50vh] overflow-y-auto pr-1">
              {(['浏览', '操作', '管理'] as const).map(group => {
                const items = permAll.filter(p => p.group === group)
                if (items.length === 0) return null
                return (
                  <div key={group}>
                    <div className="text-[11px] font-semibold text-muted-foreground mb-1.5">{group}模块</div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-1">
                      {items.map(p => {
                        const isDefault = permDefaults.includes(p.key)
                        const isGranted = permGranted.includes(p.key)
                        return (
                        <label
                          key={p.key}
                          className={`flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-[12px] transition-colors ${
                            isDefault || isGranted
                              ? 'border-primary/40 bg-primary/5 text-foreground'
                              : 'border-border/50 text-muted-foreground hover:border-border'
                          } ${isDefault ? 'cursor-default' : 'cursor-pointer'}`}
                        >
                          <input
                            type="checkbox"
                            className="accent-primary"
                            checked={isDefault || isGranted}
                            disabled={isDefault}
                            onChange={() => togglePerm(p.key)}
                          />
                          <span className="flex-1 min-w-0 truncate">{p.label}</span>
                          {isDefault && (
                            <span className="flex-shrink-0 rounded-full border border-border/50 px-1.5 py-px text-[9px] text-muted-foreground">
                              角色默认
                            </span>
                          )}
                        </label>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
              <p className="text-[10px] text-muted-foreground leading-relaxed">
                说明:勾选 = 给该用户开放对应功能模块(在角色默认权限之上)。例如给成员勾选「数据源」,该用户即可进入数据源管理页。未勾选的管理类模块维持角色默认(成员不可见)。
              </p>
            </div>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" size="sm" onClick={() => setPermTarget(null)}>取消</Button>
            <Button size="sm" onClick={() => void savePermissions()} disabled={permLoading || permSaving}>
              {permSaving ? '保存中' : '保存'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
