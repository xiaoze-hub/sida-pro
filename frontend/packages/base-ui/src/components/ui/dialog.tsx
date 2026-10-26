import * as React from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { cn } from '../../cn'

const Dialog = DialogPrimitive.Root
const DialogTrigger = DialogPrimitive.Trigger
const DialogClose = DialogPrimitive.Close

const DialogPortal = DialogPrimitive.Portal

const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      'fixed inset-0 z-50 bg-black/40 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
      className
    )}
    {...props}
  />
))
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName

const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & { placement?: 'center' | 'right' }
>(({ className, children, placement = 'center', onOpenAutoFocus, onCloseAutoFocus, ...props }, ref) => {
  const returnFocus = React.useRef<HTMLElement | null>(null)
  return (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      onOpenAutoFocus={event => {
        returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
        onOpenAutoFocus?.(event)
      }}
      onCloseAutoFocus={event => {
        onCloseAutoFocus?.(event)
        if (!event.defaultPrevented && returnFocus.current?.isConnected) {
          event.preventDefault()
          returnFocus.current.focus()
        }
      }}
      className={cn(
        placement === 'right'
          ? 'fixed inset-y-0 right-0 z-[60] h-[100dvh] w-full max-w-xl overflow-y-auto overscroll-contain border-l border-border bg-popover p-6 shadow-float'
          : 'fixed left-1/2 top-[max(1rem,env(safe-area-inset-top))] md:top-1/2 z-50 w-[calc(100vw-1rem)] max-w-lg -translate-x-1/2 md:-translate-y-1/2 bg-popover rounded-xl border border-border-strong shadow-float p-6 pr-4 max-h-[calc(100vh-2rem-env(safe-area-inset-top))] md:max-h-[calc(100vh-2rem)] overflow-y-auto overflow-x-hidden overscroll-contain scrollbar duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
        className
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close
        aria-label="关闭弹窗"
        className="ui-icon-button absolute right-3 top-3"
      >
        <X className="h-4 w-4" />
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPortal>
  )
})
DialogContent.displayName = DialogPrimitive.Content.displayName

const DialogHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('mb-5', className)} {...props} />
)
DialogHeader.displayName = 'DialogHeader'

const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn('text-[16px] font-semibold text-foreground', className)}
    {...props}
  />
))
DialogTitle.displayName = DialogPrimitive.Title.displayName

const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn('text-[14px] text-muted-foreground mt-1.5', className)}
    {...props}
  />
))
DialogDescription.displayName = DialogPrimitive.Description.displayName

export {
  Dialog,
  DialogPortal,
  DialogOverlay,
  DialogClose,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
}
