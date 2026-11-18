import { Type } from 'class-transformer';
import { IsInt, IsOptional, IsString, Matches, Max, Min } from 'class-validator';

export class CompleteUploadDto {
  @IsString({ message: 'sha256 deve ser um texto' })
  @Matches(/^[a-fA-F0-9]{64}$/, { message: 'sha256 deve ter 64 caracteres hexadecimais' })
  sha256!: string;

  @Type(() => Number)
  @IsInt({ message: 'total_parts deve ser um número inteiro' })
  @Min(0, { message: 'total_parts deve ser maior ou igual a 0' })
  total_parts!: number;
}

export class UploadPartsQueryDto {
  @IsOptional()
  @IsString({ message: 'cursor deve ser um texto' })
  cursor?: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'limit deve ser um número inteiro' })
  @Min(1, { message: 'limit deve ser maior ou igual a 1' })
  @Max(1000, { message: 'limit deve ser no máximo 1000' })
  limit?: number;
}
