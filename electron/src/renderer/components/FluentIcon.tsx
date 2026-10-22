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
import PhotoUploadSvg from '@assets/icons/ui/PhotoUpload.svg?raw'
import EditSvg from '@assets/icons/ui/Edit.svg?raw'
import WorkSvg from '@assets/icons/ui/Work.svg?raw'
import DismissSvg from '@assets/icons/ui/Dismiss.svg?raw'
import AddSvg from '@assets/icons/ui/Add.svg?raw'
import ChevronRightSvg from '@assets/icons/ui/ChevronRight.svg?raw'
import CheckmarkSvg from '@assets/icons/ui/Checkmark.svg?raw'
import PuzzlePieceSvg from '@assets/icons/ui/PuzzlePiece.svg?raw'

export type FluentIconName =
  | 'Chat' | 'Video' | 'Movie' | 'Tiles' | 'CommandPrompt'
  | 'Setting' | 'Palette' | 'Robot' | 'Camera' | 'Cut'
  | 'SpeedHigh' | 'Font' | 'Language' | 'Microphone'
  | 'Play' | 'Sync' | 'Send' | 'Pin'
  | 'LeftArrow' | 'RightArrow' | 'Album' | 'People' | 'Photo' | 'PhotoUpload'
  | 'Edit' | 'Work' | 'Dismiss' | 'Add' | 'ChevronRight' | 'Checkmark' | 'PuzzlePiece'

const ICONS: Record<FluentIconName, string> = {
  Chat: ChatSvg, Video: VideoSvg, Movie: MovieSvg, Tiles: TilesSvg,
  CommandPrompt: CommandPromptSvg, Setting: SettingSvg,
  Palette: PaletteSvg, Robot: RobotSvg, Camera: CameraSvg, Cut: CutSvg,
  SpeedHigh: SpeedHighSvg, Font: FontSvg, Language: LanguageSvg,
  Microphone: MicrophoneSvg, Play: PlaySvg, Sync: SyncSvg,
  Send: SendSvg, Pin: PinSvg, LeftArrow: LeftArrowSvg,
  RightArrow: RightArrowSvg, Album: AlbumSvg, People: PeopleSvg,
  Photo: PhotoSvg, PhotoUpload: PhotoUploadSvg, Edit: EditSvg,
  Work: WorkSvg, Dismiss: DismissSvg, Add: AddSvg, ChevronRight: ChevronRightSvg,
  Checkmark: CheckmarkSvg, PuzzlePiece: PuzzlePieceSvg,
}

/** Icon sizes follow the type scale: 14 beside small text, 16 for controls, 20 and 24 for tiles. */
export type FluentIconSize = 14 | 16 | 20 | 24

/** Icons take their color from the surrounding text (currentColor). */
interface Props extends HTMLAttributes<HTMLSpanElement> {
  name: FluentIconName
  size?: FluentIconSize
}

export default function FluentIcon({ name, size = 16, className, ...rest }: Props) {
  const svg = ICONS[name]

  // Icon files declare their own width, height and style; replace them so every icon renders at `size`.
  const sized = useMemo(() => svg
    ? svg.replace(/^[\s\S]*?<svg\b([^>]*)>/, (_, attributes: string) =>
      `<svg${attributes.replace(/\s(?:width|height|style)="[^"]*"/g, '')} width="${size}" height="${size}">`)
    : '', [svg, size])

  return (
    <span
      {...rest}
      className={className ? `fluent-icon ${className}` : 'fluent-icon'}
      dangerouslySetInnerHTML={{ __html: sized }}
    />
  )
}
