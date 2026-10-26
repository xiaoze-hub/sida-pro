import { AssistantAvatar } from './AssistantAvatar'
import { ASSISTANT_AVATARS, setAssistantAvatar, useAssistantAvatar } from '@/lib/assistant-avatar'

export function AssistantAvatarPicker() {
  const selected = useAssistantAvatar()
  return <fieldset className="mt-5 border-t border-border pt-5">
    <legend className="px-0 text-[14px] font-semibold">AI 助手形象</legend>
    <p className="mb-3 text-[12px] text-muted-foreground">浮窗、完整对话与菜单共用；空闲慢速循环并轻柔呼吸，分析和书写时恢复正常速度。保存在当前账号的本机浏览器。</p>
    <div className="grid grid-cols-2 gap-3">
      {ASSISTANT_AVATARS.map(item => <label key={item.id} className={`flex cursor-pointer flex-col items-center gap-2 rounded-xl p-4 ${selected === item.id ? 'bg-s3' : 'bg-s2 hover:bg-s3'}`}>
        <AssistantAvatar variant={item.id} size={72} />
        <span className="flex items-center gap-2 text-[14px]"><input type="radio" name="assistant-avatar" value={item.id} checked={selected === item.id} onChange={() => setAssistantAvatar(item.id)} />{item.label}{item.id === 'foriday' && <span className="text-[12px] text-muted-foreground">默认</span>}</span>
      </label>)}
    </div>
  </fieldset>
}
