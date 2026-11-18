import { Type } from 'class-transformer';
import { IsInt, IsOptional, IsString, Max, MaxLength, Min } from 'class-validator';

/** Sheet selector page. `q` filters by name; it never hides data. */
export class SheetListQueryDto {
  @IsOptional()
  @IsString({ message: 'cursor deve ser um texto' })
  @MaxLength(512)
  cursor?: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'limit deve ser um número inteiro' })
  @Min(1, { message: 'limit deve ser maior ou igual a 1' })
  @Max(200, { message: 'limit deve ser no máximo 200' })
  limit?: number;

  @IsOptional()
  @IsString({ message: 'q deve ser um texto' })
  @MaxLength(120)
  q?: string;
}

/** Paginated provenance pages for one sheet. */
export class SheetPagesQueryDto {
  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'cursor deve ser um número inteiro' })
  @Min(0, { message: 'cursor deve ser maior ou igual a 0' })
  cursor?: number;

  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'limit deve ser um número inteiro' })
  @Min(1, { message: 'limit deve ser maior ou igual a 1' })
  @Max(1000, { message: 'limit deve ser no máximo 1000' })
  limit?: number;
}
