import { IsOptional, IsString, MaxLength } from 'class-validator';

/** Local session only: the browser is identified by a device id, not an account. */
export class AnonymousSessionDto {
  @IsOptional()
  @IsString({ message: 'device_id deve ser um texto' })
  @MaxLength(128)
  device_id?: string;
}
