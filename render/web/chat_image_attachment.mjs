const VISUAL_ATTACHMENT_MAX_LONG_SIDE = 1280
const VISUAL_ATTACHMENT_JPEG_QUALITY = 0.82

function loadImageElement(src) {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('Could not decode image'))
    image.src = src
  })
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('Could not read image'))
    reader.readAsDataURL(file)
  })
}

export async function prepareImageAttachment(file) {
  if (!file.type.startsWith('image/')) {
    throw new Error('Please choose an image file.')
  }
  const originalDataUrl = await readFileAsDataUrl(file)
  const image = await loadImageElement(originalDataUrl)
  const scale = Math.min(1, VISUAL_ATTACHMENT_MAX_LONG_SIDE / Math.max(image.naturalWidth, image.naturalHeight, 1))
  const width = Math.max(1, Math.round(image.naturalWidth * scale))
  const height = Math.max(1, Math.round(image.naturalHeight * scale))
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Canvas is unavailable.')
  ctx.drawImage(image, 0, 0, width, height)
  const dataUrl = canvas.toDataURL('image/jpeg', VISUAL_ATTACHMENT_JPEG_QUALITY)
  const base64 = dataUrl.split(',', 2)[1] || ''
  const byteLength = Math.floor((base64.length * 3) / 4)
  return {
    name: file.name || 'image.jpg',
    mime: 'image/jpeg',
    dataUrl,
    width,
    height,
    byteLength,
  }
}
