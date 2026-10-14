import { useRef, useState, useCallback, type PointerEvent as ReactPointerEvent } from 'react'

export type CropRect = {
  x: number // 0.0 - 1.0
  y: number // 0.0 - 1.0
  w: number // 0.0 - 1.0
  h: number // 0.0 - 1.0
}

interface Props {
  imageSrc: string
  crop: CropRect
  onChange: (crop: CropRect) => void
  disabled?: boolean
}

type DragMode = 'move' | 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'

export default function VNCropOverlay({ imageSrc, crop, onChange, disabled }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [dragState, setDragState] = useState<{
    mode: DragMode
    startX: number
    startY: number
    startCrop: CropRect
    containerRect: DOMRect
  } | null>(null)

  const handlePointerDown = (mode: DragMode, e: ReactPointerEvent<HTMLDivElement>) => {
    if (disabled || !containerRef.current) return
    e.preventDefault()
    e.stopPropagation()
    const containerRect = containerRef.current.getBoundingClientRect()
    setDragState({
      mode,
      startX: e.clientX,
      startY: e.clientY,
      startCrop: { ...crop },
      containerRect,
    })
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const handlePointerMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragState) return
    e.preventDefault()
    const { mode, startX, startY, startCrop, containerRect } = dragState
    if (containerRect.width <= 0 || containerRect.height <= 0) return

    const dx = (e.clientX - startX) / containerRect.width
    const dy = (e.clientY - startY) / containerRect.height

    let newX = startCrop.x
    let newY = startCrop.y
    let newW = startCrop.w
    let newH = startCrop.h

    if (mode === 'move') {
      newX = Math.max(0, Math.min(1 - startCrop.w, startCrop.x + dx))
      newY = Math.max(0, Math.min(1 - startCrop.h, startCrop.y + dy))
    } else {
      // Horizontal resize
      if (mode.includes('w')) {
        const right = startCrop.x + startCrop.w
        newX = Math.max(0, Math.min(right - 0.05, startCrop.x + dx))
        newW = right - newX
      } else if (mode.includes('e')) {
        newW = Math.max(0.05, Math.min(1 - startCrop.x, startCrop.w + dx))
      }

      // Vertical resize
      if (mode.includes('n')) {
        const bottom = startCrop.y + startCrop.h
        newY = Math.max(0, Math.min(bottom - 0.05, startCrop.y + dy))
        newH = bottom - newY
      } else if (mode.includes('s')) {
        newH = Math.max(0.05, Math.min(1 - startCrop.y, startCrop.h + dy))
      }
    }

    onChange({
      x: Math.round(newX * 1000) / 1000,
      y: Math.round(newY * 1000) / 1000,
      w: Math.round(newW * 1000) / 1000,
      h: Math.round(newH * 1000) / 1000,
    })
  }, [dragState, onChange])

  const handlePointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (dragState) {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId)
      } catch {
        // ignore if pointer capture already lost
      }
      setDragState(null)
    }
  }

  // Clamped display values
  const leftPct = Math.max(0, Math.min(1, crop.x)) * 100
  const topPct = Math.max(0, Math.min(1, crop.y)) * 100
  const widthPct = Math.max(5, Math.min(100 - leftPct, crop.w * 100))
  const heightPct = Math.max(5, Math.min(100 - topPct, crop.h * 100))

  return (
    <div
      ref={containerRef}
      className="vn-crop-container"
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      style={{
        position: 'relative',
        width: '100%',
        maxHeight: '400px',
        overflow: 'hidden',
        borderRadius: '8px',
        border: '1px solid var(--border)',
        backgroundColor: '#0a0a0c',
        userSelect: 'none',
        touchAction: 'none',
      }}
    >
      <img
        src={imageSrc}
        alt="Preview Window Frame"
        style={{
          width: '100%',
          height: 'auto',
          maxHeight: '400px',
          objectFit: 'contain',
          display: 'block',
          pointerEvents: 'none',
        }}
      />

      {/* Darkened mask overlays outside the crop box */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: `${topPct}%`,
          backgroundColor: 'rgba(0, 0, 0, 0.65)',
          pointerEvents: 'none',
        }}
      />
      <div
        style={{
          position: 'absolute',
          top: `${topPct + heightPct}%`,
          left: 0,
          right: 0,
          bottom: 0,
          backgroundColor: 'rgba(0, 0, 0, 0.65)',
          pointerEvents: 'none',
        }}
      />
      <div
        style={{
          position: 'absolute',
          top: `${topPct}%`,
          left: 0,
          width: `${leftPct}%`,
          height: `${heightPct}%`,
          backgroundColor: 'rgba(0, 0, 0, 0.65)',
          pointerEvents: 'none',
        }}
      />
      <div
        style={{
          position: 'absolute',
          top: `${topPct}%`,
          left: `${leftPct + widthPct}%`,
          right: 0,
          height: `${heightPct}%`,
          backgroundColor: 'rgba(0, 0, 0, 0.65)',
          pointerEvents: 'none',
        }}
      />

      {/* Interactive selection rectangle */}
      <div
        className="vn-crop-selection"
        onPointerDown={e => handlePointerDown('move', e)}
        style={{
          position: 'absolute',
          left: `${leftPct}%`,
          top: `${topPct}%`,
          width: `${widthPct}%`,
          height: `${heightPct}%`,
          border: '2px solid #00e5ff',
          boxShadow: '0 0 10px rgba(0, 229, 255, 0.45)',
          cursor: disabled ? 'default' : 'move',
          boxSizing: 'border-box',
        }}
      >
        {/* Subtle grid lines */}
        <div
          style={{
            position: 'absolute',
            inset: 0,
            backgroundImage:
              'linear-gradient(to right, rgba(0, 229, 255, 0.2) 1px, transparent 1px), linear-gradient(to bottom, rgba(0, 229, 255, 0.2) 1px, transparent 1px)',
            backgroundSize: '33.33% 33.33%',
            pointerEvents: 'none',
          }}
        />

        {/* Informative badge */}
        <div
          style={{
            position: 'absolute',
            bottom: '6px',
            left: '6px',
            backgroundColor: 'rgba(10, 10, 15, 0.85)',
            color: '#00e5ff',
            fontSize: '11px',
            padding: '2px 6px',
            borderRadius: '4px',
            pointerEvents: 'none',
            whiteSpace: 'nowrap',
            border: '1px solid rgba(0, 229, 255, 0.3)',
          }}
        >
          {Math.round(widthPct)}% × {Math.round(heightPct)}%
        </div>

        {/* 8 resize handles */}
        {!disabled && (
          <>
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('nw', e)}
              style={{
                position: 'absolute',
                top: -6,
                left: -6,
                width: 12,
                height: 12,
                cursor: 'nwse-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('n', e)}
              style={{
                position: 'absolute',
                top: -6,
                left: '50%',
                transform: 'translateX(-50%)',
                width: 12,
                height: 12,
                cursor: 'ns-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('ne', e)}
              style={{
                position: 'absolute',
                top: -6,
                right: -6,
                width: 12,
                height: 12,
                cursor: 'nesw-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('e', e)}
              style={{
                position: 'absolute',
                top: '50%',
                right: -6,
                transform: 'translateY(-50%)',
                width: 12,
                height: 12,
                cursor: 'ew-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('se', e)}
              style={{
                position: 'absolute',
                bottom: -6,
                right: -6,
                width: 12,
                height: 12,
                cursor: 'nwse-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('s', e)}
              style={{
                position: 'absolute',
                bottom: -6,
                left: '50%',
                transform: 'translateX(-50%)',
                width: 12,
                height: 12,
                cursor: 'ns-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('sw', e)}
              style={{
                position: 'absolute',
                bottom: -6,
                left: -6,
                width: 12,
                height: 12,
                cursor: 'nesw-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
            <div
              className="vn-crop-handle"
              onPointerDown={e => handlePointerDown('w', e)}
              style={{
                position: 'absolute',
                top: '50%',
                left: -6,
                transform: 'translateY(-50%)',
                width: 12,
                height: 12,
                cursor: 'ew-resize',
                backgroundColor: '#ffffff',
                border: '2px solid #00e5ff',
                borderRadius: '2px',
              }}
            />
          </>
        )}
      </div>
    </div>
  )
}
