import { Link } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Button } from '@sida/base-ui/components/ui/button'
import PageHeader from '@/components/PageHeader'
import { SystemStatusPanel } from '@/components/system/SystemStatusPanel'
import { getJwtRole } from '@/lib/jwt'

export default function SystemStatusPage() {
  return <div className="page-container">
    <PageHeader title="系统状态" description="查看模块依赖、连接依据与最近检测结果" actions={<Button variant="ghost" asChild><Link to="/system"><ArrowLeft className="h-4 w-4" />系统管理</Link></Button>} />
    <div className="card-surface rounded-xl bg-card p-4 md:p-5"><SystemStatusPanel enabled={getJwtRole() === 'owner'} /></div>
  </div>
}
