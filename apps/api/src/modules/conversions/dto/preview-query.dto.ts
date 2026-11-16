import { Type } from 'class-transformer';
import { IsInt, IsOptional, IsString, Max, MaxLength, Min } from 'class-validator';

export class PreviewQueryDto {
  @IsOptional()
  @IsString({ message: 'sheet deve ser um texto' })
  @MaxLength(120)
  sheet?: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'offset deve ser um número inteiro' })
  @Min(0, { message: 'offset deve ser maior ou igual a 0' })
  offset?: number;

  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'limit deve ser um número inteiro' })
  @Min(1, { message: 'limit deve ser maior ou igual a 1' })
  @Max(1000, { message: 'limit deve ser no máximo 1000' })
  limit?: number;
}
