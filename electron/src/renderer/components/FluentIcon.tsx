import { useMemo, type HTMLAttributes } from 'react'

// Raw SVG imports (processed by Vite ?raw)
import ChatSvg from '@assets/icons/ui/Chat.svg?raw'
import VideoSvg from '@assets/icons/ui/Video.svg?raw'
import MovieSvg from '@assets/icons/ui/Movie.svg?raw'
import TilesSvg from '@assets/icons/ui/Tiles.svg?raw'
import CommandPromptSvg from '@assets/icons/ui/CommandPrompt.svg?raw'
import SettingSvg from '@assets/icons/ui/Setting.svg?raw'
import PaletteSvg from '@assets/icons/ui/Palette.svg?raw'
import RobotSvg from '@assets/icons/ui/Robot.svg?raw'
import CameraSvg from '@assets/icons/ui/Camera.svg?raw'
import CutSvg from '@assets/icons/ui/Cut.svg?raw'
import SpeedHighSvg from '@assets/icons/ui/SpeedHigh.svg?raw'
import FontSvg from '@assets/icons/ui/Font.svg?raw'
import LanguageSvg from '@assets/icons/ui/Language.svg?raw'
import MicrophoneSvg from '@assets/icons/ui/Microphone.svg?raw'
import PlaySvg from '@assets/icons/ui/Play.svg?raw'
import SyncSvg from '@assets/icons/ui/Sync.svg?raw'
import SendSvg from '@assets/icons/ui/Send.svg?raw'
import PinSvg from '@assets/icons/ui/Pin.svg?raw'
import LeftArrowSvg from '@assets/icons/ui/LeftArrow.svg?raw'
import RightArrowSvg from '@assets/icons/ui/RightArrow.svg?raw'
import AlbumSvg from '@assets/icons/ui/Album.svg?raw'
import PeopleSvg from '@assets/icons/ui/People.svg?raw'
import PhotoSvg from '@assets/icons/ui/Photo.svg?raw'
import EditSvg from '@assets/icons/ui/Edit.svg?raw'
import WorkSvg from '@assets/icons/ui/Work.svg?raw'

export type FluentIconName =
  | 'Chat' | 'Video' | 'Movie' | 'Tiles' | 'CommandPrompt'
  | 'Setting' | 'Palette' | 'Robot' | 'Camera' | 'Cut'
  | 'SpeedHigh' | 'Font' | 'Language' | 'Microphone'
  | 'Play' | 'Sync' | 'Send' | 'Pin'
  | 'LeftArrow' | 'RightArrow' | 'Album' | 'People' | 'Photo'
  | 'Edit' | 'Work'

const ICONS: Record<FluentIconName, string> = {
  Chat: ChatSvg, Video: VideoSvg, Movie: MovieSvg, Tiles: TilesSvg,
  CommandPrompt: CommandPromptSvg, Setting: SettingSvg,
  Palette: PaletteSvg, Robot: RobotSvg, Camera: CameraSvg, Cut: CutSvg,
  SpeedHigh: SpeedHighSvg, Font: FontSvg, Language: LanguageSvg,
  Microphone: MicrophoneSvg, Play: PlaySvg, Sync: SyncSvg,
  Send: SendSvg, Pin: PinSvg, LeftArrow: LeftArrowSvg,
  RightArrow: RightArrowSvg, Album: AlbumSvg, People: PeopleSvg,
  Photo: PhotoSvg, Edit: EditSvg,
  Work: WorkSvg,
}

interface Props extends Omit<HTMLAttributes<HTMLSpanElement>, 'color'> {
  name: FluentIconName
  size?: number
  color?: string
}

export default function FluentIcon({ name, size = 18, color, style, ...rest }: Props) {
  const svg = ICONS[name]

  // inject width/height into the SVG string
  const sized = useMemo(() => {
    if (!svg) return ''
    return svg.replace(
      /<svg([^>]*)>/,
      `<svg$1 width="${size}" height="${size}" style="flex-shrink:0" >`
    )
  }, [svg, size])

  return (
    <span
      {...rest}
      style={{ display: 'inline-flex', alignItems: 'center', color: color || 'inherit', ...style }}
      dangerouslySetInnerHTML={{ __html: sized }}
    />
  )
}
