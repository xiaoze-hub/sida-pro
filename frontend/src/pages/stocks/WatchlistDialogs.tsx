import { Button } from '@sida/base-ui/components/ui/button'
import { Dialog } from '@sida/base-ui/components/ui/dialog'
import { DialogContent } from '@sida/base-ui/components/ui/dialog'
import { DialogDescription } from '@sida/base-ui/components/ui/dialog'
import { DialogHeader } from '@sida/base-ui/components/ui/dialog'
import { DialogTitle } from '@sida/base-ui/components/ui/dialog'
import { Input } from '@sida/base-ui/components/ui/input'
import { Label } from '@sida/base-ui/components/ui/label'
import { useStocks } from './context'

export function WatchlistDialogs() {
  const {
    accountDialogOpen,
    setAccountDialogOpen,
    accountForm,
    setAccountForm,
    editAccountId,
    removeWatchStock,
    setRemoveWatchStock,
    removingWatchStock,
    hasAnyPositionForStockId,
    removeFromWatchlist,
    handleAccountSubmit,
  } = useStocks()
  return (
    <>
{/* Remove Watchlist Dialog */}
<Dialog open={!!removeWatchStock} onOpenChange={(open) => { if (!open) setRemoveWatchStock(null) }}>
  <DialogContent className="max-w-md">
    <DialogHeader>
      <DialogTitle>删除股票</DialogTitle>
      <DialogDescription>删除后将从系统中移除该股票及其关注配置</DialogDescription>
    </DialogHeader>
    {removeWatchStock && (
      <div className="space-y-4 mt-2">
        <div className="card-surface rounded-md bg-s2 p-3">
          <div className="text-[14px] font-semibold text-foreground">
            {removeWatchStock.name}
            <span className="ml-2 font-mono text-[12px] text-muted-foreground">{removeWatchStock.symbol}</span>
          </div>
          <div className="mt-1 text-[12px] text-muted-foreground">
            {hasAnyPositionForStockId(removeWatchStock.id)
              ? '该股票存在持仓，不能直接删除。请先在“持仓”Tab 删除持仓记录。'
              : '删除后将不再出现在关注列表，同时会清理该股票关联的价格提醒。'}
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setRemoveWatchStock(null)} disabled={removingWatchStock}>取消</Button>
          <Button
            variant="destructive"
            onClick={() => removeFromWatchlist(removeWatchStock)}
            disabled={removingWatchStock || hasAnyPositionForStockId(removeWatchStock.id)}
          >
            {hasAnyPositionForStockId(removeWatchStock.id) ? '请先删除持仓' : (removingWatchStock ? '处理中…' : '删除股票')}
          </Button>
        </div>
      </div>
    )}
  </DialogContent>
</Dialog>

{/* Account Dialog */}
<Dialog open={accountDialogOpen} onOpenChange={setAccountDialogOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>{editAccountId ? '编辑账户' : '添加账户'}</DialogTitle>
      <DialogDescription>设置交易账户信息</DialogDescription>
    </DialogHeader>
    <div className="space-y-4 mt-2">
      <div>
        <Label>账户名称</Label>
        <Input
          value={accountForm.name}
          onChange={e => setAccountForm({ ...accountForm, name: e.target.value })}
          placeholder="如：招商证券、华泰证券"
        />
      </div>
      <div>
        <Label>可用资金（元）</Label>
        {/* 编辑态 placeholder 不许写「0」：那会让人以为「空 = 0 元现金」。
            空在编辑语义下是「保持原值不变」（后端 PUT 没有「清空成未知」的语义）。 */}
        <Input
          value={accountForm.available_funds}
          onChange={e => setAccountForm({ ...accountForm, available_funds: e.target.value })}
          placeholder={editAccountId ? '留空 = 保持原值' : '0'}
          className="font-mono"
          inputMode="decimal"
          data-testid="account-funds-input"
        />
        <p className="mt-1 text-[12px] text-muted-foreground" data-testid="account-funds-hint">
          {editAccountId
            ? '留空表示保持已登记金额不变（后端不支持把已登记金额清空为「未知」）。'
            : '留空将按 0 元登记：0 表示「账上确实没有现金」，不是「未知」。'}
        </p>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={() => setAccountDialogOpen(false)}>取消</Button>
        <Button onClick={handleAccountSubmit} disabled={!accountForm.name}>
          {editAccountId ? '保存' : '创建'}
        </Button>
      </div>
    </div>
  </DialogContent>
</Dialog>
    </>
  )
}
