import { useState } from 'react'
import { AlertCircle, Play, RotateCcw } from 'lucide-react'
import { Button } from '@sida/base-ui/components/ui/button'

type ProductVideoProps = {
  title: string
  src: string
  poster: string
  captions: string
  duration: string
  portrait?: boolean
}

/** 封面先于视频加载；仅在点击后创建播放器，切换演示时卸载以停止播放。 */
export function ProductVideo({ title, src, poster, captions, duration, portrait = false }: ProductVideoProps) {
  const [started, setStarted] = useState(false)
  const [failed, setFailed] = useState(false)
  const [posterFailed, setPosterFailed] = useState(false)
  return (
    <div className="rounded-xl bg-s2 p-2 sm:p-3">
      <div className={`relative overflow-hidden rounded-xl ${portrait ? 'aspect-[390/844]' : 'aspect-[3/2]'}`}>
        {started && !failed ? (
          <video src={src} controls autoPlay playsInline preload="metadata" poster={poster} aria-label={`${title}操作视频`} onError={() => setFailed(true)} className="absolute inset-0 h-full w-full rounded-xl object-contain">
            <track kind="captions" src={captions} srcLang="zh" label="中文操作说明" default />
          </video>
        ) : (
          <>
            {!posterFailed && <img src={poster} alt={`${title}视频预览`} width={portrait ? 390 : 1440} height={portrait ? 844 : 960} loading="lazy" onError={() => setPosterFailed(true)} className="absolute inset-0 h-full w-full rounded-xl object-cover" />}
            <div className="absolute inset-0 flex items-center justify-center bg-black/20">
              {failed || posterFailed ? (
                <div role="status" className="mx-4 max-w-sm rounded-xl bg-popover p-5 text-center text-foreground shadow-sm">
                  <AlertCircle className="mx-auto mb-3 text-muted-foreground" size={24} />
                  <p className="text-[14px]">{failed ? '视频暂时无法播放' : '视频封面暂时无法加载'}</p>
                  <div className="mt-4 flex flex-wrap justify-center gap-2">
                    <Button onClick={() => { setFailed(false); setPosterFailed(false); setStarted(true) }}><RotateCcw size={16} />{failed ? '重新播放' : '直接播放'}</Button>
                    <Button variant="outline" asChild><a href={src} download>下载视频</a></Button>
                  </div>
                </div>
              ) : (
                <Button onClick={() => setStarted(true)} variant="ghost" aria-label={`播放${title}演示`} className="h-14 gap-3 rounded-full bg-card px-5 text-foreground shadow-sm hover:bg-accent"><span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-primary-foreground"><Play size={16} fill="currentColor" /></span><span className="pr-1 font-medium">播放演示</span></Button>
              )}
            </div>
          </>
        )}
      </div>
      <div className="flex items-center justify-between gap-3 px-1 pb-1 pt-3 text-[12px] text-muted-foreground"><span className="font-medium text-foreground">{title} · 实际操作</span><span className="shrink-0 font-mono">{duration}</span></div>
    </div>
  )
}
